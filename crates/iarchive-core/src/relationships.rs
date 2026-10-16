//! Observed local relationships, never filename-based ownership or cloud coverage.
use crate::{FileFact, FileKind, collision_key};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const VERSION: &str = "iarchive-relationships-v1";
pub const EXTRACTOR_VERSION: &str = "13.59";

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Metadata {
    pub content_id: Option<String>,
    pub image_id: Option<String>,
    pub document_id: Option<String>,
    pub original_document_id: Option<String>,
    pub derived_from: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Member {
    pub id: i64,
    pub fact: FileFact,
    pub metadata: Metadata,
    pub role: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Group {
    pub fingerprint: String,
    pub members: Vec<Member>,
    pub blockers: Vec<String>,
}

impl Metadata {
    fn keys(&self) -> Vec<String> {
        let mut keys = vec![];
        for (namespace, value) in [
            ("content", &self.content_id),
            ("image", &self.image_id),
            ("document", &self.document_id),
            ("document", &self.original_document_id),
            ("document", &self.derived_from),
        ] {
            if let Some(value) = value {
                keys.push(format!("{namespace}:{value}"));
            }
        }
        keys
    }
}

pub fn group(mut members: Vec<Member>) -> Vec<Group> {
    members.sort_by(|a, b| a.fact.relative_path.cmp(&b.fact.relative_path));
    let mut parents: Vec<_> = (0..members.len()).collect();
    fn root(parents: &[usize], mut i: usize) -> usize {
        while parents[i] != i {
            i = parents[i];
        }
        i
    }
    let mut keys = BTreeMap::new();
    for (i, member) in members.iter().enumerate() {
        // Failed observations never establish an edge, even if partial tags exist.
        if member.metadata.error.is_some() || member.fact.error.is_some() {
            continue;
        }
        for key in member.metadata.keys() {
            if let Some(previous) = keys.insert(key, i) {
                let a = root(&parents, previous);
                let b = root(&parents, i);
                parents[b] = a;
            }
        }
    }
    let mut groups: BTreeMap<usize, Vec<Member>> = BTreeMap::new();
    let mut unknown_stems = BTreeSet::new();
    let unassigned = members.iter().any(|m| {
        m.metadata.keys().is_empty()
            || m.metadata.error.is_some()
            || m.fact.error.is_some()
            || !matches!(
                m.fact.kind,
                FileKind::Image | FileKind::Video | FileKind::Sidecar
            )
    });
    for (i, mut member) in members.into_iter().enumerate() {
        if member.metadata.keys().is_empty() || member.metadata.error.is_some() {
            unknown_stems.insert(stem(&member.fact.relative_path));
        }
        member.role = match member.fact.kind {
            FileKind::Video if member.metadata.content_id.is_some() => "Live Photo motion",
            FileKind::Image if member.metadata.derived_from.is_some() => "adjusted image",
            FileKind::Image if member.metadata.content_id.is_some() => {
                "Live Photo still / image variant"
            }
            FileKind::Image if member.metadata.document_id.is_some() => "original / document image",
            FileKind::Image => "identified image",
            FileKind::Sidecar => "metadata sidecar",
            _ => "unsupported variant",
        }
        .into();
        groups.entry(root(&parents, i)).or_default().push(member);
    }
    groups
        .into_values()
        .map(|members| {
            let mut blockers = BTreeSet::new();
            if unassigned {blockers.insert("Archive contains unassigned variants; logical group completeness is NOT VERIFIED".into());}
            let documents: BTreeSet<_> = members
                .iter()
                .filter_map(|m| m.metadata.document_id.as_deref())
                .collect();
            let mut contents: BTreeMap<&str, Vec<&Member>> = BTreeMap::new();
            for member in &members {
                let m = &member.metadata;
                if m.error.is_some()
                    || member.fact.error.is_some()
                    || member.fact.sha256.is_none()
                    || !matches!(
                        member.fact.kind,
                        FileKind::Image | FileKind::Video | FileKind::Sidecar
                    )
                {
                    blockers.insert(
                        "Unreadable, incomplete, or unsupported relationship member".into(),
                    );
                }
                if m.keys().is_empty() {
                    blockers.insert(
                        "Associated file relationships are unknown; no metadata identity".into(),
                    );
                }
                for parent in [&m.derived_from,&m.original_document_id].into_iter().flatten() {
                    if !documents.contains(parent.as_str()) {
                        blockers.insert("Referenced original document is missing".into());
                    }
                }
                if let Some(id) = &m.content_id {
                    contents.entry(id).or_default().push(member);
                }
                if unknown_stems.contains(&stem(&member.fact.relative_path)) {
                    blockers.insert(
                        "Possible same-name companion has unknown metadata; files remain separate"
                            .into(),
                    );
                }
            }
            for parts in contents.values() {
                let stills: Vec<_> = parts
                    .iter()
                    .filter(|m| m.fact.kind == FileKind::Image)
                    .collect();
                let videos = parts
                    .iter()
                    .filter(|m| m.fact.kind == FileKind::Video)
                    .count();
                if stills.is_empty() || videos != 1 {
                    blockers.insert(
                        "Live Photo still/motion relationship is missing or ambiguous".into(),
                    );
                }
                if stills.len() > 1 {
                    let images: BTreeSet<_> = stills
                        .iter()
                        .filter_map(|m| m.metadata.image_id.as_deref())
                        .collect();
                    if images.len() != 1 || stills.iter().any(|m| m.metadata.image_id.is_none()) {
                        blockers
                            .insert("Multiple still variants lack a common image identity".into());
                    }
                }
            }
            let fingerprint = format!(
                "{:x}",
                Sha256::digest(
                    serde_json::to_vec(&(VERSION, &members))
                        .expect("serializable relationship members")
                )
            );
            Group {
                fingerprint,
                members,
                blockers: blockers.into_iter().collect(),
            }
        })
        .collect()
}

fn stem(path: &str) -> String {
    // A name collision is only a blocker. Names never create a relationship.
    collision_key(path.rsplit_once('.').map_or(path, |(stem, _)| stem))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn member(id: i64, path: &str, kind: FileKind, metadata: Metadata) -> Member {
        Member {
            id,
            metadata,
            role: String::new(),
            fact: FileFact {
                relative_path: path.into(),
                path_key: collision_key(path),
                kind,
                byte_count: 1,
                mtime_ns: "1".into(),
                sha256: Some("a".repeat(64)),
                error: None,
                warnings: vec![],
            },
        }
    }
    #[test]
    fn metadata_edges_preserve_variants() {
        let input = vec![
            member(
                1,
                "still.heic",
                FileKind::Image,
                Metadata {
                    content_id: Some("live".into()),
                    image_id: Some("image".into()),
                    ..Metadata::default()
                },
            ),
            member(
                2,
                "different-name.mov",
                FileKind::Video,
                Metadata {
                    content_id: Some("live".into()),
                    ..Metadata::default()
                },
            ),
            member(
                3,
                "original.jpg",
                FileKind::Image,
                Metadata {
                    document_id: Some("original".into()),
                    ..Metadata::default()
                },
            ),
            member(
                4,
                "crop.jpg",
                FileKind::Image,
                Metadata {
                    document_id: Some("crop".into()),
                    derived_from: Some("original".into()),
                    ..Metadata::default()
                },
            ),
            member(
                5,
                "sidecar.xmp",
                FileKind::Sidecar,
                Metadata {
                    document_id: Some("original".into()),
                    ..Metadata::default()
                },
            ),
        ];
        let groups = group(input.clone());
        assert_eq!(
            groups.iter().map(|g| g.members.len()).collect::<Vec<_>>(),
            vec![3, 2]
        );
        assert!(groups.iter().all(|g| g.blockers.is_empty()));
        assert_eq!(groups.iter().flat_map(|g| g.members.iter()).count(), 5);
        let mut reversed = input;
        reversed.reverse();
        assert_eq!(
            group(reversed),
            groups,
            "ordering must not change group evidence"
        );
    }
    #[test]
    fn unproven_groups_stay_blocked() {
        for (input, reason) in [
            (
                vec![
                    member(1, "same.jpg", FileKind::Image, Metadata::default()),
                    member(2, "same.mov", FileKind::Video, Metadata::default()),
                ],
                "unknown",
            ),
            (
                vec![member(
                    1,
                    "still.heic",
                    FileKind::Image,
                    Metadata {
                        content_id: Some("live".into()),
                        ..Metadata::default()
                    },
                )],
                "missing or ambiguous",
            ),
            (
                vec![member(
                    1,
                    "crop.jpg",
                    FileKind::Image,
                    Metadata {
                        derived_from: Some("missing".into()),
                        ..Metadata::default()
                    },
                )],
                "original document is missing",
            ),
            (
                vec![
                    member(
                        1,
                        "still.heic",
                        FileKind::Image,
                        Metadata {
                            content_id: Some("live".into()),
                            ..Metadata::default()
                        },
                    ),
                    member(
                        2,
                        "one.mov",
                        FileKind::Video,
                        Metadata {
                            content_id: Some("live".into()),
                            ..Metadata::default()
                        },
                    ),
                    member(
                        3,
                        "two.mov",
                        FileKind::Video,
                        Metadata {
                            content_id: Some("live".into()),
                            ..Metadata::default()
                        },
                    ),
                ],
                "missing or ambiguous",
            ),
        ] {
            let count = input.len();
            let groups = group(input);
            assert_eq!(groups.iter().map(|g| g.members.len()).sum::<usize>(), count);
            assert!(
                groups
                    .iter()
                    .all(|g| g.blockers.iter().any(|b| b.contains(reason))),
                "{reason}"
            );
            if reason == "unknown" {
                assert_eq!(groups.len(), 2, "names must never create an edge");
            }
        }
    }
}
