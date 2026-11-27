use std::env;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

fn resolve_python_target() -> Option<(PathBuf, PathBuf, Vec<String>, Option<String>)> {
    // 1. Packaged app bundle: <App>/Contents/Helpers/jardctl
    if let Ok(exe) = env::current_exe().and_then(|p| p.canonicalize()) {
        if let Some(helpers) = exe.parent() {
            if let Some(contents) = helpers.parent() {
                let bundled_python = contents.join("Resources/PythonRuntime/bin/python3.13");
                let bundled_helper = contents.join("Resources/engine/desktop/build-tools/helper.py");
                if bundled_python.is_file() && bundled_helper.is_file() {
                    return Some((
                        bundled_python,
                        bundled_helper,
                        vec!["-EsBu".to_string()],
                        None,
                    ));
                }
            }
        }
        // Check if running from desktop/cli/target/{release,debug}/jaro-cli or desktop/.build/cli-target/...
        for ancestor in exe.ancestors() {
            let candidate_script = ancestor.join("engine/jardctl.py");
            if candidate_script.is_file() {
                let python = find_dev_python(ancestor);
                let engine_dir = ancestor.join("engine");
                let pythonpath = merge_pythonpath(&engine_dir, ancestor);
                return Some((python, candidate_script, Vec::new(), Some(pythonpath)));
            }
        }
    }

    // 2. Explicit override or current working directory checkout
    if let Ok(cwd) = env::current_dir() {
        for ancestor in cwd.ancestors() {
            let candidate_script = ancestor.join("engine/jardctl.py");
            if candidate_script.is_file() {
                let python = find_dev_python(ancestor);
                let engine_dir = ancestor.join("engine");
                let pythonpath = merge_pythonpath(&engine_dir, ancestor);
                return Some((python, candidate_script, Vec::new(), Some(pythonpath)));
            }
        }
    }

    None
}

fn find_dev_python(repo_root: &Path) -> PathBuf {
    if let Ok(py) = env::var("JARO_PYTHON") {
        if !py.is_empty() {
            return PathBuf::from(py);
        }
    }
    let venv_py = repo_root.join(".venv/bin/python");
    if venv_py.is_file() {
        return venv_py;
    }
    PathBuf::from("python3")
}

fn merge_pythonpath(engine: &Path, repo: &Path) -> String {
    let mut parts = vec![engine.to_string_lossy().into_owned(), repo.to_string_lossy().into_owned()];
    if let Ok(existing) = env::var("PYTHONPATH") {
        if !existing.is_empty() {
            parts.push(existing);
        }
    }
    parts.join(":")
}

/// Note: for packaged `helper.py`, prepend `"control"` before user args.
pub fn exec_python_fallback_with_mode(args: &[String]) -> ! {
    if let Some((python, script, mut extra_args, pythonpath)) = resolve_python_target() {
        let is_bundled_helper = script.file_name().and_then(|n| n.to_str()) == Some("helper.py");
        let mut cmd = Command::new(&python);
        if is_bundled_helper {
            cmd.env("PYTHONNOUSERSITE", "1");
            cmd.env("PYTHONDONTWRITEBYTECODE", "1");
            for a in extra_args.drain(..) {
                cmd.arg(a);
            }
            cmd.arg(&script);
            cmd.arg("control");
        } else {
            for a in extra_args.drain(..) {
                cmd.arg(a);
            }
            cmd.arg(&script);
        }
        cmd.args(args);
        if let Some(pp) = pythonpath {
            cmd.env("PYTHONPATH", pp);
        }
        let err = cmd.exec();
        eprintln!("Failed to execute Python CLI fallback ({}): {err}", python.display());
        std::process::exit(1);
    }
    eprintln!("Could not locate the Jaro Python CLI runtime.");
    std::process::exit(1);
}
