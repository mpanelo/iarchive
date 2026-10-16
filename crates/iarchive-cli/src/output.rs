use crate::Commands;
use serde_json::Value;
use std::collections::BTreeMap;

pub fn summary(command: &Commands, value: &Value) -> Option<String> {
    if matches!(command, Commands::Cycles) {
        let cycles = value["cycles"].as_array()?;
        if cycles.is_empty() {
            return Some("No archive cycles. Import a local archive to begin.".into());
        }
        let mut lines = vec!["Saved archive cycles:".into()];
        for status in cycles {
            lines.push(format!(
                "  {}: {}, {} files, {} bytes; inventory {}",
                text(&status["cycle"]),
                text(&status["stage"]),
                status["file_count"],
                status["bytes"],
                inventory(status)
            ));
        }
        lines.push("Saved state only; current local bytes and cloud coverage NOT VERIFIED.".into());
        return Some(lines.join("\n"));
    }
    let status = match command {
        Commands::Status { .. } => value,
        Commands::Index { .. } | Commands::Audit { .. } => &value["status"],
        _ => return None,
    };
    let mut lines = vec![
        format!(
            "Cycle {}: {}",
            text(&status["cycle"]),
            text(&status["stage"])
        ),
        format!(
            "{} files, {} bytes; inventory {}",
            status["file_count"],
            status["bytes"],
            inventory(status)
        ),
        format!(
            "Images: {} analyzed, {} failed, {} pending",
            status["image_successes"], status["image_errors"], status["pending_images"]
        ),
    ];
    if status["unsupported_visual_files"]
        .as_u64()
        .is_some_and(|n| n > 0)
    {
        lines.push(format!(
            "{} files not visually audited",
            status["unsupported_visual_files"]
        ));
    }
    if ["file_errors", "missing_files", "scan_issues"]
        .iter()
        .any(|key| status[key].as_u64().is_some_and(|n| n > 0))
    {
        lines.push(format!(
            "Findings: {} file errors, {} missing originals, {} scan issues",
            status["file_errors"], status["missing_files"], status["scan_issues"]
        ));
    }
    if !matches!(command, Commands::Status { .. }) {
        lines.push(format!(
            "Inventory hashes: {} read, {} cached",
            value["hashes_computed"], value["hash_cache_hits"]
        ));
        match command {
            Commands::Audit { .. } => {
                lines.push(format!(
                    "Analysis: {} decoded, {} cached, {} decode errors",
                    value["decoded_images"], value["analysis_cache_hits"], value["decode_errors"]
                ));
                let mut counts = BTreeMap::new();
                for group in value["candidates"].as_array()? {
                    *counts.entry(text(&group["type"])).or_insert(0usize) += 1;
                }
                let groups = counts
                    .into_iter()
                    .map(|(kind, count)| format!("{kind}: {count}"))
                    .collect::<Vec<_>>();
                lines.push(format!(
                    "Review groups: {}",
                    if groups.is_empty() {
                        "none".into()
                    } else {
                        groups.join(", ")
                    }
                ));
                lines.push(format!(
                    "Review suggestions with iarchive review --cycle {}",
                    text(&status["cycle"])
                ));
            }
            Commands::Index { .. } => {
                lines.push(format!(
                    "Exact duplicate groups: {}",
                    value["exact_duplicate_groups"].as_array()?.len()
                ));
            }
            _ => unreachable!(),
        }
        if let Some(error) = value["scan_error"].as_str() {
            lines.push(format!("Scan error: {error:?}"));
        }
        for finding in value["findings"].as_array()? {
            for key in ["file_error", "decode_error"] {
                if let Some(error) = finding[key].as_str() {
                    lines.push(format!(
                        "  {:?}: {error:?}",
                        text(&finding["relative_path"])
                    ));
                }
            }
        }
        for issue in value["scan_issues"].as_array()? {
            lines.push(format!(
                "  {:?}: {:?}",
                text(&issue["path"]),
                text(&issue["error"])
            ));
        }
    }
    for job in status["quarantine_jobs"].as_array()? {
        lines.push(format!(
            "Quarantine {}: {}",
            text(&job["id"]),
            text(&job["state"])
        ));
        if let Some(error) = job["error"].as_str() {
            lines.push(format!("  {error:?}"));
        }
    }
    lines.push(format!(
        "Last replica run: {} (recorded only)",
        status["replica_state"].as_str().unwrap_or("none")
    ));
    lines.push("Replica bytes: NOT VERIFIED by this command; use verify --deep.".into());
    lines.push("Cloud coverage: NOT VERIFIED".into());
    if matches!(command, Commands::Status { .. }) {
        lines.push("Saved state only; no media was checked.".into());
        if status["inventory_complete"] != true
            || status["image_errors"].as_u64().is_some_and(|n| n > 0)
        {
            lines.push(format!(
                "See findings with iarchive report --cycle {}",
                text(&status["cycle"])
            ));
        }
    }
    Some(lines.join("\n"))
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("unknown")
}

fn inventory(status: &Value) -> &'static str {
    if status["inventory_complete"] == true {
        "complete"
    } else {
        "incomplete"
    }
}
