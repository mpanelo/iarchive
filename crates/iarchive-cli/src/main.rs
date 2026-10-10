mod operations;
mod worker;

use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use fs2::FileExt;
use iarchive_core::{absolute_destination, ensure_separate, validate_cycle};
use iarchive_store::Store;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::ExitCode,
};

#[derive(Parser)]
#[command(
    name = "iarchive",
    version,
    about = "Read-only local photo inventory and audit. Replication and cloud coverage are NOT VERIFIED."
)]
struct Cli {
    #[arg(long, global = true)]
    state_dir: Option<PathBuf>,
    #[arg(long, global = true, help = "Emit JSON reports and structured logs")]
    json: bool,
    #[arg(
        long,
        global = true,
        help = "Describe planned work without writing state or scanning media"
    )]
    dry_run: bool,
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Check local capabilities without downloading or authenticating.
    Doctor {
        #[arg(long)]
        primary: Option<PathBuf>,
        #[arg(long)]
        secondary: Option<PathBuf>,
    },
    /// Configure existing primary and optional secondary roots; never reorganize media.
    Init {
        #[arg(long)]
        primary: PathBuf,
        #[arg(long)]
        secondary: Option<PathBuf>,
    },
    /// Register an existing archive; does not establish completed cloud coverage.
    Import {
        #[arg(long)]
        source: PathBuf,
        #[arg(long)]
        cycle: String,
    },
    /// Display last recorded local state; does not verify current media.
    Status {
        #[arg(long)]
        cycle: String,
    },
    /// Inventory and SHA-256 all local files, with resumable metadata caching.
    Index {
        #[arg(long)]
        cycle: String,
        #[arg(long, help = "Bypass hash cache")]
        deep: bool,
    },
    /// Export a versioned JSONL inventory outside the source tree.
    ExportManifest {
        #[arg(long)]
        cycle: String,
        #[arg(long)]
        output: PathBuf,
    },
    /// Analyze images locally; candidates are suggestions for human review only.
    Audit {
        #[arg(long)]
        cycle: String,
        #[arg(long, default_value = "exact,similar,dark")]
        categories: String,
        #[arg(long)]
        worker_project: Option<PathBuf>,
        #[arg(long, default_value_t = 60)]
        timeout_seconds: u64,
        #[arg(
            long,
            default_value_t = 1024,
            help = "Thumbnail cache capacity in MiB; no automatic eviction"
        )]
        cache_limit_mib: u64,
    },
    /// Display recorded candidates and decode failures.
    Report {
        #[arg(long)]
        cycle: String,
    },
}

#[derive(Debug, Serialize, Deserialize)]
struct Config {
    schema_version: u32,
    primary: PathBuf,
    secondary: Option<PathBuf>,
    free_space_margin: f64,
    icloudpd_version: String,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let filter =
        tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into());
    if cli.json {
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_writer(std::io::stderr)
            .json()
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_writer(std::io::stderr)
            .init();
    }
    match run(&cli) {
        Ok(complete) => {
            if complete {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            }
        }
        Err(e) => {
            if cli.json {
                eprintln!("{}", serde_json::json!({"error":format!("{e:#}")}));
            } else {
                eprintln!("iarchive: {e:#}");
            }
            ExitCode::from(1)
        }
    }
}

fn state_dir(cli: &Cli) -> Result<PathBuf> {
    let p = cli
        .state_dir
        .clone()
        .or_else(|| {
            directories::ProjectDirs::from("", "", "iarchive")
                .map(|p| p.data_local_dir().to_owned())
        })
        .context("cannot determine app-state directory; use --state-dir")?;
    absolute_destination(&p)
}

fn load_config(state: &Path) -> Result<Option<Config>> {
    let path = state.join("config.toml");
    if !path.exists() {
        return Ok(None);
    }
    let c: Config = toml::from_str(&fs::read_to_string(path)?)?;
    ensure!(
        c.schema_version == 1 && c.free_space_margin >= 0.0 && c.free_space_margin < 1.0,
        "unsupported or invalid config"
    );
    Ok(Some(c))
}

fn check_root(state: &Path, root: &Path) -> Result<()> {
    let root = absolute_destination(root)?;
    ensure!(
        !state.starts_with(&root) && !root.starts_with(state),
        "state and media roots must not overlap"
    );
    Ok(())
}

fn preflight(state: &Path, config: &Option<Config>) -> Result<()> {
    for name in [
        "config.toml",
        "manifest.sqlite",
        "manifest.sqlite-wal",
        "manifest.sqlite-shm",
        "writer.lock",
        "thumbnails",
    ] {
        let path = state.join(name);
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            ensure!(
                !iarchive_core::is_link(&metadata),
                "app-state artifact is a symlink/reparse point: {name}"
            );
        }
    }
    if let Some(c) = config {
        check_root(state, &c.primary)?;
        if let Some(s) = &c.secondary {
            check_root(state, s)?;
        }
    }
    let db = state.join("manifest.sqlite");
    if db.exists() {
        // Check before opening a writable SQLite connection or acquiring the lock.
        let conn = rusqlite_readonly(&db)?;
        let roots = conn.roots()?;
        for root in roots {
            check_root(state, &root)?;
        }
    }
    Ok(())
}

fn rusqlite_readonly(path: &Path) -> Result<Store> {
    Store::open_readonly(path)
}

fn lock_state(state: &Path) -> Result<File> {
    fs::create_dir_all(state)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(state.join("writer.lock"))?;
    file.try_lock_exclusive()
        .context("another iarchive operation holds this state directory")?;
    Ok(file)
}

fn emit(cli: &Cli, value: &impl Serialize) -> Result<()> {
    if cli.json {
        println!("{}", serde_json::to_string(value)?);
    } else {
        println!("{}", serde_json::to_string_pretty(value)?);
    }
    Ok(())
}

fn run(cli: &Cli) -> Result<bool> {
    let state = state_dir(cli)?;
    let config = load_config(&state)?;
    preflight(&state, &config)?;
    match &cli.command {
        Commands::Doctor { primary, secondary } => {
            return operations::doctor(
                cli,
                &state,
                primary
                    .as_deref()
                    .or(config.as_ref().map(|c| c.primary.as_path())),
                secondary
                    .as_deref()
                    .or(config.as_ref().and_then(|c| c.secondary.as_deref())),
                config
                    .as_ref()
                    .map(|c| c.icloudpd_version.as_str())
                    .unwrap_or("1.32.3"),
            );
        }
        Commands::Init { primary, secondary } => {
            let primary = primary
                .canonicalize()
                .context("primary archive is unavailable")?;
            ensure!(primary.is_dir(), "primary must be an existing directory");
            ensure_separate(&state, &primary)?;
            let secondary = secondary.as_deref().map(absolute_destination).transpose()?;
            if let Some(s) = &secondary {
                check_root(&state, s)?;
                ensure!(
                    !s.starts_with(&primary) && !primary.starts_with(s),
                    "primary and secondary must not overlap"
                );
            }
            if cli.dry_run {
                emit(
                    cli,
                    &serde_json::json!({"dry_run":true,"operation":"init","primary":primary,"secondary":secondary}),
                )?;
                return Ok(true);
            }
            let _lock = lock_state(&state)?;
            let c = Config {
                schema_version: 1,
                primary,
                secondary,
                free_space_margin: 0.15,
                icloudpd_version: "1.32.3".into(),
            };
            if let Some(existing) = config {
                ensure!(
                    existing.primary == c.primary && existing.secondary == c.secondary,
                    "configuration already exists with different roots; edit config explicitly"
                );
            } else {
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(state.join("config.toml"))?;
                use std::io::Write;
                file.write_all(toml::to_string_pretty(&c)?.as_bytes())?;
                file.sync_all()?;
            }
            Store::open(&state.join("manifest.sqlite"))?;
            emit(
                cli,
                &serde_json::json!({"configured":true,"state_dir":state,"local_replica":"NOT VERIFIED","cloud_coverage":"NOT VERIFIED"}),
            )?;
            return Ok(true);
        }
        Commands::Import { source, cycle } => {
            validate_cycle(cycle)?;
            let source = source
                .canonicalize()
                .context("source archive is unavailable")?;
            ensure!(source.is_dir(), "source must be a directory");
            ensure_separate(&state, &source)?;
            if let Some(s) = config.as_ref().and_then(|c| c.secondary.as_deref()) {
                let s = absolute_destination(s)?;
                ensure!(
                    !source.starts_with(&s) && !s.starts_with(&source),
                    "source overlaps configured secondary"
                );
            }
            if cli.dry_run {
                emit(
                    cli,
                    &serde_json::json!({"dry_run":true,"operation":"import","cycle":cycle,"source":source}),
                )?;
                return Ok(true);
            }
            let _lock = lock_state(&state)?;
            let mut store = Store::open(&state.join("manifest.sqlite"))?;
            store.import(cycle, &source)?;
            emit(cli, &store.status(cycle)?)?;
            return Ok(true);
        }
        _ => {}
    }
    let cycle = match &cli.command {
        Commands::Status { cycle }
        | Commands::Index { cycle, .. }
        | Commands::ExportManifest { cycle, .. }
        | Commands::Audit { cycle, .. }
        | Commands::Report { cycle } => cycle,
        _ => unreachable!(),
    };
    validate_cycle(cycle)?;
    ensure!(
        state.join("manifest.sqlite").exists(),
        "no state database; import a synthetic archive first"
    );
    if cli.dry_run {
        let store = Store::open_readonly(&state.join("manifest.sqlite"))?;
        let root = store.root(cycle)?;
        if let Commands::ExportManifest { output, .. } = &cli.command {
            operations::validate_export(output, &store, &state)?;
        }
        emit(
            cli,
            &serde_json::json!({"dry_run":true,"cycle":cycle,"source":root,"will_write_media":false}),
        )?;
        return Ok(true);
    }
    let _lock = lock_state(&state)?;
    let mut store = Store::open(&state.join("manifest.sqlite"))?;
    match &cli.command {
        Commands::Status { .. } => {
            emit(cli, &store.status(cycle)?)?;
            Ok(true)
        }
        Commands::Index { deep, .. } => operations::index(cli, &mut store, cycle, *deep),
        Commands::ExportManifest { output, .. } => {
            operations::export(cli, &store, cycle, output, &state)
        }
        Commands::Audit {
            categories,
            worker_project,
            timeout_seconds,
            cache_limit_mib,
            ..
        } => operations::audit(
            cli,
            &mut store,
            cycle,
            &state,
            categories,
            worker_project.as_deref(),
            *timeout_seconds,
            *cache_limit_mib,
        ),
        Commands::Report { .. } => operations::report(cli, &store, cycle),
        _ => unreachable!(),
    }
}
