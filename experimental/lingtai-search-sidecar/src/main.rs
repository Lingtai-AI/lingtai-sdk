//! Optional Rust sidecar for LingTai file search.
//!
//! Speaks a tiny JSON protocol over stdin/stdout:
//!
//! ```json
//! { "op": "grep" | "glob",
//!   "root": "<sandbox root>",
//!   "path": "<search path under root>",
//!   "pattern": "<regex or glob>",
//!   "max_results": 50,
//!   "max_visited": 20000,
//!   "walltime_ms": 8000,
//!   "max_file_bytes": 4194304,
//!   "exclude_dirs": [".git", "node_modules", ...] }
//! ```
//!
//! Both `op` variants return a `Response` envelope (see below). The sidecar
//! deliberately stays close in semantics to ``LocalFileIOBackend`` so the
//! Python-side ``RustFileIOBackend`` can swap them for one another without
//! changing model-facing tool schemas.
//!
//! Implementation notes
//! --------------------
//!
//! Internally we use the ripgrep stack — ``ignore`` for traversal,
//! ``globset`` for glob matching, and ``grep_searcher`` + ``grep_regex``
//! for the grep scan. The point is to get the same engineering work that
//! makes ``rg`` fast (memchr/SIMD-accelerated line splitting, binary
//! detection, fixed-string fast paths in the regex matcher) without
//! reimplementing it. Budgets (``max_results`` / ``max_visited`` /
//! ``walltime_ms`` / ``max_file_bytes``) and the explicit ``exclude_dirs``
//! list stay in lock-step with the pure-Python backend.
//!
//! Process exit codes:
//!   * 0 — `ok: true` envelope on stdout (matches/paths plus stats)
//!   * 2 — `ok: false` envelope with structured `error.code` / `error.message`
//!
//! Exit 0 + `ok: false` is never produced; callers can rely on the exit code
//! mirroring the envelope.

use globset::{Glob, GlobMatcher};
use grep_matcher::Matcher;
use grep_regex::RegexMatcherBuilder;
use grep_searcher::{Searcher, SearcherBuilder, Sink, SinkMatch};
use ignore::{DirEntry, WalkBuilder, WalkState};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Debug, Deserialize)]
struct Request {
    op: String,
    root: PathBuf,
    path: PathBuf,
    pattern: String,
    #[serde(default = "default_max_results")]
    max_results: usize,
    #[serde(default = "default_max_visited")]
    max_visited: usize,
    #[serde(default = "default_walltime_ms")]
    walltime_ms: u64,
    #[serde(default = "default_max_file_bytes")]
    max_file_bytes: u64,
    #[serde(default = "default_exclude_dirs")]
    exclude_dirs: Vec<String>,
}

fn default_max_results() -> usize {
    50
}
fn default_max_visited() -> usize {
    20_000
}
fn default_walltime_ms() -> u64 {
    8_000
}
fn default_max_file_bytes() -> u64 {
    4 * 1024 * 1024
}
fn default_exclude_dirs() -> Vec<String> {
    // Sidecar callers (the Python adapter) always pass an explicit list so
    // their defaults stay the single source of truth. This default only
    // applies if the sidecar is driven manually.
    vec![
        ".git".into(),
        ".hg".into(),
        ".svn".into(),
        "node_modules".into(),
        ".venv".into(),
        "venv".into(),
        "env".into(),
        "__pycache__".into(),
        ".pytest_cache".into(),
        ".mypy_cache".into(),
        ".ruff_cache".into(),
        "target".into(),
        "dist".into(),
        "build".into(),
        ".cache".into(),
        "history".into(),
        "tmp".into(),
        "daemons".into(),
        ".notification".into(),
    ]
}

#[derive(Debug, Serialize)]
struct GrepMatch {
    path: String,
    line_number: usize,
    line: String,
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    code: String,
    message: String,
}

#[derive(Debug, Serialize)]
struct Response {
    ok: bool,
    backend: &'static str,
    op: String,
    matches: Vec<GrepMatch>,
    paths: Vec<String>,
    visited: usize,
    files_skipped_size: usize,
    files_skipped_binary: usize,
    dirs_pruned: usize,
    elapsed_ms: u128,
    truncated_reason: Option<&'static str>,
    error: Option<ErrorBody>,
}

fn empty_response(op: &str) -> Response {
    Response {
        ok: false,
        backend: "lingtai-search-sidecar",
        op: op.to_string(),
        matches: Vec::new(),
        paths: Vec::new(),
        visited: 0,
        files_skipped_size: 0,
        files_skipped_binary: 0,
        dirs_pruned: 0,
        elapsed_ms: 0,
        truncated_reason: None,
        error: None,
    }
}

fn main() {
    let response = match run() {
        Ok(resp) => resp,
        Err((op, code, message)) => {
            let mut resp = empty_response(&op);
            resp.error = Some(ErrorBody { code, message });
            resp
        }
    };
    println!(
        "{}",
        serde_json::to_string(&response).expect("serialize response")
    );
    if !response.ok {
        std::process::exit(2);
    }
}

fn run() -> Result<Response, (String, String, String)> {
    let mut input = String::new();
    io::Read::read_to_string(&mut io::stdin(), &mut input)
        .map_err(|err| ("?".into(), "stdin".into(), format!("read stdin: {err}")))?;
    let request: Request = serde_json::from_str(&input).map_err(|err| {
        (
            "?".into(),
            "bad_request".into(),
            format!("parse request: {err}"),
        )
    })?;

    let op_label = request.op.clone();
    let root = canonicalize_existing(&request.root)
        .map_err(|err| (op_label.clone(), "bad_root".into(), err))?;
    let path = canonicalize_existing(&request.path)
        .map_err(|err| (op_label.clone(), "bad_path".into(), err))?;
    if !path.starts_with(&root) {
        return Err((
            op_label,
            "sandbox_escape".into(),
            "path escapes root".into(),
        ));
    }

    let excludes: HashSet<String> = request.exclude_dirs.iter().cloned().collect();
    let start = Instant::now();
    let mut response = empty_response(&request.op);
    response.ok = true;

    match request.op.as_str() {
        "grep" => {
            // ``grep_regex`` is the same regex engine ``rg`` uses; it picks
            // fast paths (literal / anchored / Unicode-class) automatically.
            let matcher = RegexMatcherBuilder::new()
                .build(&request.pattern)
                .map_err(|err| {
                    (
                        op_label.clone(),
                        "bad_pattern".into(),
                        format!("invalid regex: {err}"),
                    )
                })?;
            let outcome = grep(&root, &path, &matcher, &request, &excludes, &start);
            response.matches = outcome.matches;
            apply_stats(&mut response, &outcome.stats);
        }
        "glob" => {
            let glob = Glob::new(&request.pattern)
                .map_err(|err| {
                    (
                        op_label.clone(),
                        "bad_pattern".into(),
                        format!("invalid glob: {err}"),
                    )
                })?
                .compile_matcher();
            let outcome = glob_walk(&path, &glob, &request, &excludes, &start);
            let mut paths = outcome.paths;
            paths.sort();
            response.paths = paths;
            apply_stats(&mut response, &outcome.stats);
        }
        other => {
            return Err((
                op_label,
                "unsupported_op".into(),
                format!("unsupported op: {other}"),
            ));
        }
    }

    response.elapsed_ms = start.elapsed().as_millis();
    Ok(response)
}

#[derive(Default, Clone)]
struct Stats {
    visited: usize,
    files_skipped_size: usize,
    files_skipped_binary: usize,
    dirs_pruned: usize,
    truncated_reason: Option<&'static str>,
}

fn apply_stats(resp: &mut Response, stats: &Stats) {
    resp.visited = stats.visited;
    resp.files_skipped_size = stats.files_skipped_size;
    resp.files_skipped_binary = stats.files_skipped_binary;
    resp.dirs_pruned = stats.dirs_pruned;
    resp.truncated_reason = stats.truncated_reason;
}

struct GlobOutcome {
    paths: Vec<String>,
    stats: Stats,
}

struct GrepOutcome {
    matches: Vec<GrepMatch>,
    stats: Stats,
}

fn canonicalize_existing(path: &Path) -> Result<PathBuf, String> {
    path.canonicalize()
        .map_err(|err| format!("canonicalize {}: {err}", path.display()))
}

fn over_walltime(start: &Instant, walltime_ms: u64) -> bool {
    if walltime_ms == 0 {
        return true;
    }
    start.elapsed().as_millis() as u64 > walltime_ms
}

/// Worker count for the parallel walker.
///
/// Empirically four worker threads is the sweet spot on macOS for a mixed
/// workload of ``open`` + small-file ``read`` + ``memchr`` line scan: it
/// claws back most of the parallelism win without inviting the descriptor-
/// table / inode-lock contention that ``ignore``'s default (one worker per
/// logical CPU) hits past ~8 workers. The override is honored by
/// ``WalkBuilder::threads``.
fn num_threads() -> usize {
    4
}

/// Build a configured ``WalkBuilder`` that mirrors the pure-Python backend's
/// traversal behavior: hidden files are visible, ``.gitignore`` is *not*
/// honored, and our explicit ``exclude_dirs`` list is the only pruning rule.
///
/// We attribute pruned directories via the shared ``dirs_pruned`` counter so
/// ``Stats`` reports them just like the old hand-written walker did.
fn build_walker(
    start_path: &Path,
    excludes: &HashSet<String>,
    dirs_pruned: Arc<AtomicUsize>,
) -> WalkBuilder {
    let mut builder = WalkBuilder::new(start_path);
    // We don't want ``ignore`` to apply its standard gitignore / hidden /
    // parents filters — the pure-Python backend doesn't, and the
    // ``exclude_dirs`` list is the single source of truth.
    builder.standard_filters(false);
    builder.hidden(false);
    builder.git_ignore(false);
    builder.git_global(false);
    builder.git_exclude(false);
    builder.parents(false);
    builder.follow_links(false);
    // Cap worker threads. ``ignore``'s default ``threads(0)`` spawns one
    // worker per logical CPU, which on macOS hurts on the lots-of-tiny-files
    // workloads we see most: contention on the per-process file descriptor
    // table and parent directory inode locks costs more than parallelism
    // buys. Four workers consistently outperforms both sequential and the
    // unbounded default across the benchmarks in ``reports/file-io-bench``.
    builder.threads(num_threads());

    let excludes_owned: HashSet<String> = excludes.clone();
    builder.filter_entry(move |entry| {
        // Only prune *directories* — never prune leaf entries by name, even
        // if a file happens to share a name in ``exclude_dirs`` (matches the
        // Python ``os.walk(...)`` pruning, which only edits ``dirnames``).
        if !entry.file_type().map(|ft| ft.is_dir()).unwrap_or(false) {
            return true;
        }
        let Some(name) = entry.file_name().to_str() else {
            return true;
        };
        if excludes_owned.contains(name) {
            dirs_pruned.fetch_add(1, Ordering::Relaxed);
            false
        } else {
            true
        }
    });
    builder
}

#[derive(Default)]
struct WalkShared {
    visited: AtomicUsize,
    matches_count: AtomicUsize,
    files_skipped_size: AtomicUsize,
    files_skipped_binary: AtomicUsize,
    dirs_pruned: Arc<AtomicUsize>,
    truncated: Mutex<Option<&'static str>>,
    stop: std::sync::atomic::AtomicBool,
}

impl WalkShared {
    fn new() -> Self {
        Self {
            visited: AtomicUsize::new(0),
            matches_count: AtomicUsize::new(0),
            files_skipped_size: AtomicUsize::new(0),
            files_skipped_binary: AtomicUsize::new(0),
            dirs_pruned: Arc::new(AtomicUsize::new(0)),
            truncated: Mutex::new(None),
            stop: std::sync::atomic::AtomicBool::new(false),
        }
    }

    fn set_truncated(&self, reason: &'static str) {
        let mut slot = self.truncated.lock().expect("truncated lock poisoned");
        if slot.is_none() {
            *slot = Some(reason);
        }
        self.stop.store(true, Ordering::Relaxed);
    }

    fn should_stop(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    fn into_stats(self) -> Stats {
        Stats {
            visited: self.visited.load(Ordering::Relaxed),
            files_skipped_size: self.files_skipped_size.load(Ordering::Relaxed),
            files_skipped_binary: self.files_skipped_binary.load(Ordering::Relaxed),
            dirs_pruned: self.dirs_pruned.load(Ordering::Relaxed),
            truncated_reason: *self.truncated.lock().expect("truncated lock poisoned"),
        }
    }
}

fn rel_to(base: &Path, target: &Path) -> String {
    target
        .strip_prefix(base)
        .unwrap_or(target)
        .to_string_lossy()
        .replace('\\', "/")
}

type LineMatch = (usize, String);
type FileMatches = Vec<(PathBuf, Vec<LineMatch>)>;

/// Search files under ``start_path`` for lines matching ``matcher``.
///
/// Per-file budgets:
/// * files larger than ``max_file_bytes`` are skipped (counted as ``files_skipped_size``).
/// * files with a NUL byte in their head or that aren't UTF-8 are skipped
///   (counted as ``files_skipped_binary``). ``grep_searcher``'s
///   ``BinaryDetection::quit`` handles the NUL case during scan; the UTF-8
///   gate matches the Python backend, which reads each file via
///   ``read_text(encoding="utf-8")``.
///
/// Walker budgets: ``max_visited``, ``walltime_ms``, ``max_results`` short-
/// circuit the walk (truncated_reason set on the first trip).
fn grep(
    root: &Path,
    start_path: &Path,
    matcher: &grep_regex::RegexMatcher,
    request: &Request,
    excludes: &HashSet<String>,
    start: &Instant,
) -> GrepOutcome {
    let shared = Arc::new(WalkShared::new());
    let matches: Arc<Mutex<FileMatches>> = Arc::new(Mutex::new(Vec::new()));

    let builder = build_walker(start_path, excludes, Arc::clone(&shared.dirs_pruned));

    let max_results = request.max_results;
    let max_visited = request.max_visited;
    let max_file_bytes = request.max_file_bytes;
    let walltime_ms = request.walltime_ms;

    // ``ignore`` lets the walker run in parallel when we build it with
    // ``build_parallel`` — that's the same path ``rg`` uses. Each thread
    // gets its own ``Searcher`` to avoid contention on internal buffers.
    builder.build_parallel().run(|| {
        let shared = Arc::clone(&shared);
        let matches = Arc::clone(&matches);
        let matcher = matcher.clone();
        let mut searcher: Searcher = SearcherBuilder::new().line_number(true).build();
        Box::new(move |entry_result: Result<DirEntry, ignore::Error>| {
            if shared.should_stop() {
                return WalkState::Quit;
            }
            let visited = shared.visited.fetch_add(1, Ordering::Relaxed) + 1;
            if visited > max_visited {
                shared.set_truncated("visited");
                return WalkState::Quit;
            }
            if over_walltime(start, walltime_ms) {
                shared.set_truncated("walltime");
                return WalkState::Quit;
            }
            let Ok(entry) = entry_result else {
                return WalkState::Continue;
            };
            // ``filter_entry`` already pruned directories we care about; here
            // we only run the matcher on real files.
            let Some(ft) = entry.file_type() else {
                return WalkState::Continue;
            };
            if !ft.is_file() {
                return WalkState::Continue;
            }
            let metadata = match entry.metadata() {
                Ok(meta) => meta,
                Err(_) => return WalkState::Continue,
            };
            if metadata.len() > max_file_bytes {
                shared.files_skipped_size.fetch_add(1, Ordering::Relaxed);
                return WalkState::Continue;
            }

            let path = entry.path().to_path_buf();
            // Fast path for the "no hits in this file" case. Reading the
            // file once and asking the matcher ``is_match`` lets the regex
            // engine use its literal / aho-corasick prefilter on the whole
            // buffer instead of doing per-line work. For workloads where
            // most files do *not* match (the common case), this avoids the
            // line-splitting / line-numbering overhead entirely.
            //
            // The buffer also gives us NUL detection in one place: if the
            // head looks binary, count the skip and move on. This mirrors
            // the pure-Python backend's "skip on read_text(utf-8) error"
            // step without re-reading the file.
            let bytes = match std::fs::read(&path) {
                Ok(b) => b,
                Err(err) => {
                    if err.kind() == io::ErrorKind::PermissionDenied {
                        shared.files_skipped_binary.fetch_add(1, Ordering::Relaxed);
                    }
                    return WalkState::Continue;
                }
            };
            let head_len = bytes.len().min(4096);
            if bytes[..head_len].contains(&0u8) {
                shared.files_skipped_binary.fetch_add(1, Ordering::Relaxed);
                return WalkState::Continue;
            }
            // Mirror the pure-Python behavior of ``read_text(encoding="utf-8")``:
            // if the bytes aren't valid UTF-8, count as binary-skipped.
            if std::str::from_utf8(&bytes).is_err() {
                shared.files_skipped_binary.fetch_add(1, Ordering::Relaxed);
                return WalkState::Continue;
            }
            let any_match = matcher.is_match(&bytes).unwrap_or(false);
            if !any_match {
                return WalkState::Continue;
            }
            let mut collected: Vec<(usize, String)> = Vec::new();
            let mut sink = MatchSink {
                matches: &mut collected,
            };
            // ``search_slice`` skips the binary detection / mmap setup we
            // just did and goes straight to line splitting via ``memchr``.
            if searcher.search_slice(&matcher, &bytes, &mut sink).is_err() {
                return WalkState::Continue;
            }
            if collected.is_empty() {
                return WalkState::Continue;
            }

            // Commit this file's matches under the shared lock, watching the
            // global ``max_results`` cap so we never over-report.
            let mut sink = matches.lock().expect("match lock poisoned");
            for (line_number, line) in collected.into_iter() {
                let current = shared.matches_count.fetch_add(1, Ordering::Relaxed) + 1;
                if current > max_results {
                    shared.set_truncated("max_results");
                    return WalkState::Quit;
                }
                if let Some(last) = sink.last_mut() {
                    if last.0 == path {
                        last.1.push((line_number, line));
                        continue;
                    }
                }
                sink.push((path.clone(), vec![(line_number, line)]));
            }
            WalkState::Continue
        })
    });

    let stats = Arc::try_unwrap(shared)
        .unwrap_or_else(|_| panic!("shared walker state leaked"))
        .into_stats();

    let raw = Arc::try_unwrap(matches)
        .expect("match collection leaked")
        .into_inner()
        .expect("match lock poisoned");
    // Sort results deterministically so test assertions and Python-side
    // comparisons stay stable regardless of walker threading.
    let mut by_file: Vec<(PathBuf, Vec<(usize, String)>)> = raw;
    by_file.sort_by(|a, b| a.0.cmp(&b.0));

    let mut emitted: Vec<GrepMatch> = Vec::new();
    'outer: for (path, mut lines) in by_file {
        lines.sort_by_key(|(n, _)| *n);
        let rel = rel_to(root, &path);
        for (line_number, line) in lines {
            emitted.push(GrepMatch {
                path: rel.clone(),
                line_number,
                line,
            });
            if emitted.len() >= request.max_results {
                break 'outer;
            }
        }
    }

    GrepOutcome {
        matches: emitted,
        stats,
    }
}

/// Sink that collects every matched line for a single file. Binary / UTF-8
/// rejection happens before the sink runs, so this stays a tight per-line
/// loop — the same separation ``rg`` itself uses between "file decision"
/// and "line collection".
struct MatchSink<'a> {
    matches: &'a mut Vec<(usize, String)>,
}

impl<'a> Sink for MatchSink<'a> {
    type Error = io::Error;

    fn matched(&mut self, _searcher: &Searcher, mat: &SinkMatch<'_>) -> Result<bool, Self::Error> {
        let line_number = mat.line_number().unwrap_or(0) as usize;
        // ``mat.bytes()`` always includes the trailing newline if there was
        // one; the pure-Python backend uses ``splitlines()`` which strips
        // it. Drop a single trailing ``\n`` / ``\r\n`` here so the two
        // paths produce identical ``line`` strings.
        let bytes = mat.bytes();
        let mut end = bytes.len();
        if end > 0 && bytes[end - 1] == b'\n' {
            end -= 1;
            if end > 0 && bytes[end - 1] == b'\r' {
                end -= 1;
            }
        }
        let line = String::from_utf8_lossy(&bytes[..end]).to_string();
        self.matches.push((line_number, line));
        Ok(true)
    }
}

/// Walk ``start_path`` and emit absolute paths matching ``glob``.
///
/// Mirrors the pure-Python ``LocalFileIOBackend.glob`` semantics: ``*`` may
/// cross ``/`` (so ``**/*.py`` matches both ``a.py`` and ``src/a.py``),
/// directories listed in ``exclude_dirs`` are pruned, and budgets short-
/// circuit the walk.
fn glob_walk(
    start_path: &Path,
    glob: &GlobMatcher,
    request: &Request,
    excludes: &HashSet<String>,
    start: &Instant,
) -> GlobOutcome {
    let shared = Arc::new(WalkShared::new());
    let paths: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let builder = build_walker(start_path, excludes, Arc::clone(&shared.dirs_pruned));

    let max_results = request.max_results;
    let max_visited = request.max_visited;
    let walltime_ms = request.walltime_ms;

    builder.build_parallel().run(|| {
        let shared = Arc::clone(&shared);
        let paths = Arc::clone(&paths);
        let glob = glob.clone();
        Box::new(move |entry_result: Result<DirEntry, ignore::Error>| {
            if shared.should_stop() {
                return WalkState::Quit;
            }
            let visited = shared.visited.fetch_add(1, Ordering::Relaxed) + 1;
            if visited > max_visited {
                shared.set_truncated("visited");
                return WalkState::Quit;
            }
            if over_walltime(start, walltime_ms) {
                shared.set_truncated("walltime");
                return WalkState::Quit;
            }
            let Ok(entry) = entry_result else {
                return WalkState::Continue;
            };
            let Some(ft) = entry.file_type() else {
                return WalkState::Continue;
            };
            if !ft.is_file() {
                return WalkState::Continue;
            }
            let abs = entry.path();
            let rel = rel_to(start_path, abs);
            if !glob.is_match(&rel) {
                return WalkState::Continue;
            }
            let current = shared.matches_count.fetch_add(1, Ordering::Relaxed) + 1;
            if current > max_results {
                shared.set_truncated("max_results");
                return WalkState::Quit;
            }
            let mut sink = paths.lock().expect("glob path lock poisoned");
            sink.push(abs.to_string_lossy().replace('\\', "/"));
            WalkState::Continue
        })
    });

    let stats = Arc::try_unwrap(shared)
        .unwrap_or_else(|_| panic!("shared walker state leaked"))
        .into_stats();
    let mut collected = Arc::try_unwrap(paths)
        .expect("glob path collection leaked")
        .into_inner()
        .expect("glob path lock poisoned");
    // If we tripped ``max_results`` the parallel walker may have already
    // queued one or two extra paths before all workers saw the stop signal;
    // trim them so the caller never sees over the cap.
    if collected.len() > max_results {
        collected.truncate(max_results);
    }
    GlobOutcome {
        paths: collected,
        stats,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering as Order2};
    use std::time::{SystemTime, UNIX_EPOCH};

    /// RAII temp dir — avoids pulling in ``tempfile`` just for a handful of
    /// integration-style unit tests. Each test gets a fresh path under
    /// ``$TMPDIR`` and the tree is removed on Drop.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let seq = COUNTER.fetch_add(1, Order2::Relaxed);
            let pid = std::process::id();
            let path =
                std::env::temp_dir().join(format!("lingtai-sidecar-test-{pid}-{nanos}-{seq}"));
            fs::create_dir_all(&path).expect("create temp dir");
            Self(path)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn write_file(path: &Path, content: &[u8]) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("parent dir");
        }
        fs::write(path, content).expect("write file");
    }

    fn make_request(op: &str, root: &Path) -> Request {
        Request {
            op: op.to_string(),
            root: root.to_path_buf(),
            path: root.to_path_buf(),
            pattern: String::new(),
            max_results: 50,
            max_visited: 20_000,
            walltime_ms: 8_000,
            max_file_bytes: 4 * 1024 * 1024,
            exclude_dirs: default_exclude_dirs(),
        }
    }

    /// ``ignore::WalkBuilder`` should leave files alone even when their
    /// names appear in ``exclude_dirs`` — pruning is by-directory only, to
    /// match the Python ``os.walk(...)`` behavior.
    #[test]
    fn exclude_dirs_prunes_dirs_not_files() {
        let tmp = TempDir::new();
        let root = tmp.path();
        write_file(&root.join("src/a.py"), b"alpha\nneedle\n");
        write_file(&root.join(".git/HEAD"), b"ref: refs/heads/main\n");
        // A file *named* ``.git`` (not a directory) must still be visited.
        write_file(&root.join("notes/.git"), b"needle in plain sight\n");

        let mut req = make_request("grep", root);
        req.pattern = "needle".into();
        let matcher = RegexMatcherBuilder::new().build("needle").unwrap();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = grep(root, root, &matcher, &req, &excludes, &Instant::now());
        let paths: Vec<&str> = outcome.matches.iter().map(|m| m.path.as_str()).collect();
        // No match from inside ``.git/`` …
        assert!(paths.iter().all(|p| !p.starts_with(".git/")));
        // … but the *file* named ``.git`` is still searched and matches.
        assert!(paths.contains(&"notes/.git"));
        assert!(paths.contains(&"src/a.py"));
        assert!(outcome.stats.dirs_pruned >= 1);
    }

    /// Binary detection mirrors the pure-Python ``read_text(utf-8)`` skip:
    /// a NUL byte in the head should bump ``files_skipped_binary``, not
    /// crash the search or emit a fake match.
    #[test]
    fn nul_byte_files_are_skipped_as_binary() {
        let tmp = TempDir::new();
        let root = tmp.path();
        write_file(&root.join("text.txt"), b"needle in plain text\n");
        write_file(&root.join("bin.dat"), b"needle\x00inside binary");

        let mut req = make_request("grep", root);
        req.pattern = "needle".into();
        let matcher = RegexMatcherBuilder::new().build("needle").unwrap();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = grep(root, root, &matcher, &req, &excludes, &Instant::now());
        let paths: Vec<&str> = outcome.matches.iter().map(|m| m.path.as_str()).collect();
        assert_eq!(paths, vec!["text.txt"]);
        assert!(outcome.stats.files_skipped_binary >= 1);
    }

    /// ``max_file_bytes`` skips oversized files without reading them.
    #[test]
    fn oversize_files_are_skipped() {
        let tmp = TempDir::new();
        let root = tmp.path();
        write_file(&root.join("small.txt"), b"needle\n");
        let big = vec![b'.'; 1024];
        write_file(&root.join("big.txt"), &big);

        let mut req = make_request("grep", root);
        req.pattern = "needle".into();
        req.max_file_bytes = 64;
        let matcher = RegexMatcherBuilder::new().build("needle").unwrap();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = grep(root, root, &matcher, &req, &excludes, &Instant::now());
        let paths: Vec<&str> = outcome.matches.iter().map(|m| m.path.as_str()).collect();
        assert_eq!(paths, vec!["small.txt"]);
        assert!(outcome.stats.files_skipped_size >= 1);
    }

    /// Trailing newlines must not leak into the ``line`` field — the Python
    /// backend uses ``splitlines()`` which strips them, and downstream
    /// callers rely on that.
    #[test]
    fn match_line_strips_trailing_newline() {
        let tmp = TempDir::new();
        let root = tmp.path();
        write_file(&root.join("a.txt"), b"first\nneedle here\nlast\n");
        write_file(&root.join("crlf.txt"), b"needle on crlf\r\nend\r\n");

        let mut req = make_request("grep", root);
        req.pattern = "needle".into();
        let matcher = RegexMatcherBuilder::new().build("needle").unwrap();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = grep(root, root, &matcher, &req, &excludes, &Instant::now());
        for m in &outcome.matches {
            assert!(!m.line.ends_with('\n'));
            assert!(!m.line.ends_with('\r'));
        }
        let by_path: std::collections::HashMap<_, _> = outcome
            .matches
            .iter()
            .map(|m| (m.path.as_str(), m.line.as_str()))
            .collect();
        assert_eq!(by_path.get("a.txt"), Some(&"needle here"));
        assert_eq!(by_path.get("crlf.txt"), Some(&"needle on crlf"));
    }

    /// Glob patterns honor Python ``fnmatch`` semantics — ``*`` crosses
    /// ``/`` — and absolute paths come back sorted.
    #[test]
    fn glob_returns_absolute_paths() {
        let tmp = TempDir::new();
        let root = tmp.path();
        write_file(&root.join("src/a.py"), b"");
        write_file(&root.join("src/b.py"), b"");
        write_file(&root.join("tests/c.py"), b"");
        write_file(&root.join("README.md"), b"");

        let mut req = make_request("glob", root);
        req.pattern = "**/*.py".into();
        let glob = Glob::new(&req.pattern).unwrap().compile_matcher();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = glob_walk(root, &glob, &req, &excludes, &Instant::now());
        let mut paths = outcome.paths.clone();
        paths.sort();
        // All paths are absolute and end with .py.
        assert!(paths.iter().all(|p| Path::new(p).is_absolute()));
        assert!(paths.iter().all(|p| p.ends_with(".py")));
        // README.md is excluded by the glob.
        assert_eq!(paths.len(), 3);
    }

    /// ``max_results`` bounds the response and marks ``truncated_reason``.
    #[test]
    fn grep_respects_max_results() {
        let tmp = TempDir::new();
        let root = tmp.path();
        for i in 0..20 {
            write_file(&root.join(format!("f_{i:02}.txt")), b"needle\n");
        }
        let mut req = make_request("grep", root);
        req.pattern = "needle".into();
        req.max_results = 5;
        let matcher = RegexMatcherBuilder::new().build("needle").unwrap();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = grep(root, root, &matcher, &req, &excludes, &Instant::now());
        assert!(outcome.matches.len() <= 5);
        // The traversal must have flagged the truncation so the Python
        // adapter can surface ``truncated_reason`` to the LLM.
        assert_eq!(outcome.stats.truncated_reason, Some("max_results"));
    }

    /// ``walltime_ms == 0`` matches the Python ``walltime_s=0`` test case:
    /// the budget is over before any work happens.
    #[test]
    fn walltime_zero_short_circuits() {
        let tmp = TempDir::new();
        let root = tmp.path();
        for i in 0..5 {
            write_file(&root.join(format!("f_{i}.txt")), b"needle\n");
        }
        let mut req = make_request("glob", root);
        req.pattern = "**/*".into();
        req.walltime_ms = 0;
        let glob = Glob::new(&req.pattern).unwrap().compile_matcher();
        let excludes: HashSet<String> = req.exclude_dirs.iter().cloned().collect();
        let outcome = glob_walk(root, &glob, &req, &excludes, &Instant::now());
        assert_eq!(outcome.stats.truncated_reason, Some("walltime"));
    }
}
