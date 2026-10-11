"""Run only newly generated fixtures, never accept an archive path or account."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

from fixtures import Fixture
from prototype import Blocked, approval_phrase, execute_simulation, hash_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true",
                        help="After creating synthetic fixtures, ask for the exact mock confirmation")
    args = parser.parse_args()
    # Canonicalize only the system temp base before creating our owned directory.
    directory = Path(tempfile.mkdtemp(prefix="iarchive-cloud-offline-",
                                     dir=Path(tempfile.gettempdir()).resolve()))
    fixture = Fixture(directory)
    before = {str(path): hash_file(path) for root in fixture.roots for path in root.rglob("*") if path.is_file()}
    plan = fixture.plan()
    plan.save(fixture.state / "plan.json")
    print(f"Synthetic fixture directory: {directory}")
    print(f"Plan: {plan.data()['count']} mock assets; {plan.data()['bytes']} synthetic bytes")
    print(f"Plan SHA-256: {plan.sha256}")
    result = {"mode": "OFFLINE_SIMULATION_ONLY", "plan_created": True,
              "physical_drive_independence": "NOT VERIFIED", "live_cloud_coverage": "NOT VERIFIED"}
    try:
        if args.simulate:
            print(f"Type exactly: {approval_phrase(plan)}")
            result = execute_simulation(plan, fixture.provider, input("> "))
    except (Blocked, EOFError) as error:
        result["blocked"] = str(error)
    finally:
        exports = fixture.export_review(plan)
        after = {str(path): hash_file(path) for root in fixture.roots for path in root.rglob("*") if path.is_file()}
        result["fixture_bytes_unchanged"] = before == after
        result["review_bundles"] = [str(path) for path in exports]
        print(json.dumps(result, indent=2))
    return int("blocked" in result or not result["fixture_bytes_unchanged"])


if __name__ == "__main__":
    sys.exit(main())
