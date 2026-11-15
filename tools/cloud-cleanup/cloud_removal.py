"""Bounded Photos transport for the separate guided cleanup helper."""

import json
import logging
from unittest.mock import patch

from prepare_review import LIMITS as DRAFT_LIMITS, MODE as DRAFT_MODE, evidence
from cloud_metadata import require_provider
from prototype import Blocked, Plan, canonical
from album import CORE_KEYS, RESOURCE_KEYS, safe_record
from changes import ChangesThrottled, text, valid_zone
from conflict import PHOTO_QUERY_FLAGS, Pacer, bounded_response
from probe import allowed_request, authenticated_account


MAX_TARGETS = 100
MAX_LOOKUPS = 2


class CloudRemoval:
    """One exact soft-delete request; recovery lookups cannot resume writes.

    The caller must supply approved authentication, fresh preflight and backup
    checks, confirmation, and a durable intent before calling remove(). Neither
    a saved draft nor this transport establishes permission to remove anything.
    """

    def __init__(self, service, root, zones, draft, pacer, *, recovery_only=False):
        require_provider()
        try:
            if type(draft) is not Plan or type(pacer) is not Pacer:
                raise Blocked('A sealed saved review is required')
            data = draft.data()
            if (data.get('mode') != DRAFT_MODE or data.get('schema') != 1
                    or data.get('limits') != DRAFT_LIMITS or type(recovery_only) is not bool):
                raise Blocked('Saved review format changed')
            scope = evidence.object_value(data['scope'])
            primary = [item['zone_id'] for item in zones.get('private', [])
                       if not item.get('deleted') and item['zone_id'].get('zoneName') == 'PrimarySync']
            root = root.rstrip('/')
            if (scope['database'] != 'private' or not valid_zone(scope['zone_id'])
                    or primary != [scope['zone_id']] or authenticated_account(service) != scope['account_id']
                    or root != service._get_webservice_url('ckdatabasews').rstrip('/')
                    or not allowed_request('POST', root + '/database/1/com.apple.photos.cloud/production/private/zones/list', True, root)):
                raise Blocked('Authenticated scope differs from the saved review')
            assets, selected = evidence.records(data['assets']), data['selected']
            ids = [evidence.identity(a['id']) for a in assets]
            if (not isinstance(selected, list) or any(not text(key) for key in selected)
                    or not 1 <= len(selected) <= MAX_TARGETS or len(set(ids)) != len(ids)
                    or len(set(selected)) != len(selected) or not set(selected) <= set(ids)
                    or type(data['count']) is not int or data['count'] != len(selected)):
                raise Blocked('Selection is empty, ambiguous or exceeds its bound')
            targets = [a for a in assets if a['id'] in selected]
            for asset in targets:
                evidence.identity(asset['asset_change_tag'])
                evidence.identity(asset['master_change_tag'])
            self._masters = {a['id']: evidence.identity(a['master_id']) for a in targets}
            if (set(ids) & set(self._masters.values())
                    or any(sum(a['master_id'] == master for a in assets) != 1 for master in self._masters.values())):
                raise Blocked('Target master ownership is ambiguous')
            self._expected = {key: 'CPLAsset' for key in self._masters}
            self._expected.update({master: 'CPLMaster' for master in self._masters.values()})
            self._scope = json.loads(canonical(scope))
            self._params = json.loads(canonical(service.params))
            self._query_flags = dict(PHOTO_QUERY_FLAGS)
            self._body = canonical({'atomic': True, 'desiredKeys': ['isDeleted'], 'zoneID': scope['zone_id'],
                'operations': [{'operationType': 'update', 'record': {'recordType': 'CPLAsset',
                    'recordName': a['id'], 'recordChangeTag': evidence.identity(a['asset_change_tag']),
                    'fields': {'isDeleted': {'value': 1}}}} for a in targets]})
            self._lookup = canonical({'zoneID': scope['zone_id'],
                'records': [{'recordName': key} for key in self._expected],
                'desiredKeys': sorted(CORE_KEYS | RESOURCE_KEYS)})
        except Exception:
            raise Blocked('Removal transport requires an unambiguous saved selection and authenticated private scope') from None
        self.service, self.root, self.pacer = service, root, pacer
        self.recovery_only, self._used = recovery_only, False
        self._unavailable = False
        self.writes = self.lookups = 0
        self.finding = None

    def request(self):
        """Return a copy for the caller's frozen plan and durable journal."""
        return json.loads(self._body)

    def _check_account(self):
        if (authenticated_account(self.service) != self._scope['account_id']
                or canonical(self.service.params) != canonical(self._params)
                or self.service._get_webservice_url('ckdatabasews').rstrip('/') != self.root):
            raise Blocked('Authenticated scope changed')

    def _send(self, kind, body):
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            return self._exchange(kind, body)
        finally:
            logging.disable(previous)

    def _exchange(self, kind, body):
        from requests import Request
        from requests.adapters import HTTPAdapter
        if self._unavailable:
            raise Blocked('Cloud session is unavailable; separate recovery authentication is required')
        self._check_account()
        endpoint = self.root + '/database/1/com.apple.photos.cloud/production/private/records/' + kind
        params = {**self._params, **self._query_flags}
        with patch.object(self.service.session, 'trust_env', False):
            prepared = self.service.session.prepare_request(Request('POST', endpoint, params=params, data=body,
                                                                   headers={'Content-Type': 'application/json'}))
        expected_url = Request('POST', endpoint, params=params).prepare().url
        raw = prepared.body.encode() if isinstance(prepared.body, str) else prepared.body
        if prepared.method != 'POST' or prepared.url != expected_url or raw != body.encode():
            raise Blocked('Prepared request changed the frozen scope')
        adapter = self.service.session.get_adapter(prepared.url)
        if type(adapter) is not HTTPAdapter or adapter.max_retries.total != 0:
            raise Blocked('Transport retries and custom adapters are forbidden')
        self.pacer.wait()
        self._check_account()
        # Consume before send: a lost reply never makes a write reusable.
        self.writes += kind == 'modify'
        self.lookups += kind == 'lookup'
        response = HTTPAdapter.send(adapter, prepared, stream=True, verify=True, cert=None, proxies={}, timeout=(10, 30))
        try:
            status, _, value = bounded_response(response)
            if status in (401, 403):
                self._unavailable = True
            self._check_account()
            return status, value
        finally:
            response.close()

    def remove(self, request):
        if self.recovery_only or self._used:
            raise Blocked('Removal is unavailable or already attempted; never retry')
        self._used = True
        if canonical(request) != self._body:
            raise Blocked('Request differs from the frozen selection')
        try:
            status, value = self._send('modify', self._body)
        except (Exception, KeyboardInterrupt) as error:
            if isinstance(error, ChangesThrottled):
                self.pacer.pause = error
            self.finding = 'Removal outcome unavailable; no retry attempted'
            return self._unknown()
        records = value.get('records') if isinstance(value, dict) and not value.get('error') and not value.get('serverErrorCode') else None
        if (not isinstance(records, list) or len(records) != len(self._masters)
                or any(not isinstance(r, dict) or not text(r.get('recordName')) or r['recordName'] not in self._masters for r in records)
                or len({r['recordName'] for r in records}) != len(records)):
            self.finding = 'Removal reply identities are uncertain'
            return self._unknown()
        outcomes = []
        for record in records:
            key, reply = record['recordName'], 'UNKNOWN'
            if (record.get('recordType', 'CPLAsset') == 'CPLAsset' and record.get('deleted', False) is False
                    and not record.get('error') and ('zoneID' not in record or record['zoneID'] == self._scope['zone_id'])):
                fields = record.get('fields')
                deleted = fields.get('isDeleted') if isinstance(fields, dict) else None
                deleted = deleted.get('value') if isinstance(deleted, dict) else None
                purged = fields.get('isExpunged') if isinstance(fields, dict) else None
                purged = purged.get('value') if isinstance(purged, dict) else None
                if (200 <= status < 300 and record.get('recordType') == 'CPLAsset'
                        and not record.get('serverErrorCode') and text(record.get('recordChangeTag'))
                        and (purged is None or type(purged) is int and purged == 0)
                        and type(deleted) is int and deleted == 1):
                    reply = 'ACCEPTED'
                elif status in (200, 409) and record.get('serverErrorCode') in ('CONFLICT', 'NOT_FOUND'):
                    reply = 'REJECTED'
            outcomes.append({'id': key, 'reply': reply})
        if any(item['reply'] == 'UNKNOWN' for item in outcomes):
            self.finding = 'One or more per-record outcomes are uncertain'
        return outcomes

    def _unknown(self):
        return [{'id': key, 'reply': 'UNKNOWN'} for key in self._masters]

    def lookup(self):
        """Read only frozen target/master identities; do not infer recovery or retry."""
        if (not self.recovery_only and self.writes != 1) or self.lookups >= MAX_LOOKUPS:
            raise Blocked('Target lookup is unavailable or exhausted')
        try:
            status, value = self._send('lookup', self._lookup)
            records = value.get('records') if isinstance(value, dict) and not value.get('error') and not value.get('serverErrorCode') else None
            if (status != 200 or not isinstance(records, list) or len(records) != len(self._expected)
                    or any(not isinstance(r, dict) or not text(r.get('recordName')) or r['recordName'] not in self._expected for r in records)
                    or len({r['recordName'] for r in records}) != len(records)):
                raise Blocked('Target lookup was incomplete')
            observed = {}
            for record in records:
                key = record['recordName']
                if (record.get('recordType') != self._expected[key] or not text(record.get('recordChangeTag'))
                        or record.get('deleted', False) is not False or record.get('serverErrorCode') or record.get('error')
                        or ('zoneID' in record and record['zoneID'] != self._scope['zone_id'])):
                    raise Blocked('Target lookup returned unavailable or different records')
                cleaned = safe_record(record)
                purged = cleaned['fields'].get('isExpunged')
                if purged is not None and (type(purged) is not int or purged != 0):
                    raise Blocked('Target lookup returned a purged or ambiguous record')
                fields = cleaned['fields']
                deleted = fields.get('isDeleted')
                if deleted is not None and (type(deleted) is not int or deleted not in (0, 1)):
                    raise Blocked('Target deletion flag is ambiguous')
                if key in self._masters:
                    if fields.get('masterRef') != {'recordName': self._masters[key]} or 'isDeleted' not in fields:
                        raise Blocked('Target ownership or deletion flag is unknown')
                observed[key] = cleaned
            return [observed[key] for key in self._expected]
        except (Exception, KeyboardInterrupt) as error:
            if isinstance(error, ChangesThrottled):
                self.pacer.pause = error
            raise Blocked('Read-only target lookup unavailable, changed or throttled; no retry attempted') from None
