mod backend;
mod owner;
use backend::BackendSession;
use std::{env, error::Error, fs, path::Path, process, thread, time::Duration};

const SEED: &str = r#"
import numpy as np
import pandas as pd
mxc_value = 73
mxc_frame = pd.DataFrame({'x': [1, 2]})
mxc_array = np.array([3, 5, 7])
mxc_lambda = lambda x: x + mxc_value
mxc_file = open('/tmp/mxc-state.txt', 'w+')
mxc_file.write('durable payload')
mxc_file.flush()
mxc_file.seek(3)
"#;

const VERIFY: &str = r#"
assert mxc_value == 73, mxc_value
assert mxc_frame['x'].tolist() == [4, 2], mxc_frame
assert mxc_array.tolist() == [3, 5, 7], mxc_array
assert mxc_lambda(2) == 75
assert mxc_file.tell() == 3, mxc_file.tell()
assert mxc_file.read() == 'able payload'
mxc_file.seek(3)
print('state assertions passed')
"#;

fn write_report(path: &str, value: serde_json::Value) -> Result<(), Box<dyn Error>> {
    let destination = Path::new(path);
    let staged = destination.with_extension("part");
    fs::write(&staged, serde_json::to_vec_pretty(&value)?)?;
    fs::rename(staged, destination)?;
    Ok(())
}

fn run() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = env::args().collect();
    if args.len() == 2 && args[1] == "--help" {
        println!(
            "mxc-session-state-probe seed <initrd> <new-checkpoint> <report> [hold]\nmxc-session-state-probe restore <checkpoint> <report>\nmxc-session-state-probe call <restore> <checkpoint> <code-file> <report>\nmxc-session-state-probe call-owned <restore> <checkpoint> <code-file> <report>\nmxc-session-state-probe execute-owned <restore> <unused-candidate> <code-file> <report>"
        );
        #[cfg(feature = "bounded-output")]
        println!("mxc-session-state-probe call-bounded <restore> <checkpoint> <code-file> <report> [output-limit-bytes]");
        #[cfg(feature = "bounded-storage")]
        println!("mxc-session-state-probe call-stored <restore> <checkpoint> <code-file> <report> <output-bytes> <checkpoint-bytes> <checkpoint-files>");
        return Ok(());
    }
    match args.get(1).map(String::as_str) {
        Some("seed") if args.len() == 5 || (args.len() == 6 && args[5] == "hold") => {
            let checkpoint = Path::new(&args[3]);
            if checkpoint.exists() {
                return Err("checkpoint destination must not exist".into());
            }
            let startup = Path::new(&args[2])
                .parent()
                .ok_or("initrd has no parent")?
                .join("snapshot");
            assert!(backend::fresh_control());
            let mut sandbox = BackendSession::restore(&startup)?;
            sandbox.execute(SEED)?;
            assert!(sandbox.refuses_execution());
            let first = sandbox.capture(&checkpoint.with_extension("first"))?;
            assert!(sandbox.refuses_execution());
            sandbox.committed(&first)?;
            sandbox.execute(&format!("mxc_frame.loc[0, 'x'] = 4\n{VERIFY}"))?;
            let final_candidate = sandbox.capture(checkpoint)?;
            assert!(sandbox.refuses_execution());
            assert!(sandbox.rejects_stale(&first));
            sandbox.committed(&final_candidate)?;
            // Distinguish saved state from mutations made after capture.
            sandbox.execute("mxc_value = 999\nmxc_frame.loc[0, 'x'] = 999")?;
            write_report(
                &args[4],
                serde_json::json!({
                    "phase": "seed", "pid": process::id(), "continuity": true,
                    "snapshot_saved": true, "post_snapshot_mutation": true,
                    "commit_barrier": true, "stale_token_refused": true, "fresh_control": true,
                    "provider": "mxc-session-preview", "mxc_base": "86fb3d2abaf9c431556692037bff881830b543a5"
                }),
            )?;
            if args.len() == 6 {
                loop {
                    thread::sleep(Duration::from_secs(1));
                }
            }
        }
        Some(mode @ ("call" | "call-owned" | "execute-owned")) if args.len() == 6 => {
            if mode != "call" {
                owner::watch();
            }
            if fs::metadata(&args[4])?.len() > 65536 {
                return Err("code exceeds probe limit".into());
            }
            let code = fs::read_to_string(&args[4])?;
            let mut sandbox = BackendSession::restore(Path::new(&args[2]))?;
            sandbox.execute(&code)?;
            assert!(sandbox.refuses_execution());
            if mode == "execute-owned" {
                sandbox.close();
                write_report(&args[5], serde_json::json!({"executed": true}))?;
            } else {
                let _candidate = sandbox.capture(Path::new(&args[3]))?;
                assert!(sandbox.refuses_execution());
                sandbox.close();
                write_report(&args[5], serde_json::json!({"captured": true}))?;
            }
        }
        #[cfg(feature = "bounded-output")]
        Some(mode @ ("call-bounded" | "call-stored"))
            if (mode == "call-bounded" && (args.len() == 6 || args.len() == 7))
                || (mode == "call-stored" && args.len() == 9) =>
        {
            if mode == "call-stored" && !cfg!(feature = "bounded-storage") {
                return Err("bounded storage is not enabled".into());
            }
            owner::watch();
            let limit = args
                .get(6)
                .map(|value| value.parse::<usize>())
                .transpose()?
                .unwrap_or(1024 * 1024);
            if fs::metadata(&args[4])?.len() > 65536 {
                return Err("code exceeds probe limit".into());
            }
            let code = fs::read_to_string(&args[4])?;
            let mut sandbox = BackendSession::restore_bounded(Path::new(&args[2]), limit)?;
            sandbox.execute(&code)?;
            assert!(sandbox.refuses_execution());
            let (output, omitted, saturated) = sandbox.take_output()?;
            #[cfg(feature = "bounded-storage")]
            let _candidate = if mode == "call-stored" {
                sandbox.capture_bounded(Path::new(&args[3]), args[7].parse()?, args[8].parse()?)?
            } else {
                sandbox.capture(Path::new(&args[3]))?
            };
            #[cfg(not(feature = "bounded-storage"))]
            let _candidate = sandbox.capture(Path::new(&args[3]))?;
            assert!(sandbox.refuses_execution());
            sandbox.close();
            let destination = Path::new(&args[5]).with_extension("output");
            fs::write(&destination, output.as_bytes())?;
            write_report(
                &args[5],
                serde_json::json!({
                    "captured": true,
                    "output": {"limit_bytes": limit, "retained_bytes": output.len(),
                        "omitted_bytes": omitted, "omitted_bytes_saturated": saturated,
                        "truncated": omitted != 0}
                }),
            )?;
        }
        Some("restore") if args.len() == 4 => {
            let mut sandbox = BackendSession::restore(Path::new(&args[2]))?;
            sandbox.execute(VERIFY)?;
            assert!(sandbox.rejects_capture(Path::new(&args[2])));
            let mut failing = BackendSession::restore(Path::new(&args[2]))?;
            assert!(failing.exception());
            let mut timed = BackendSession::restore(Path::new(&args[2]))?;
            assert!(timed.timeout());
            let mut closed = BackendSession::restore(Path::new(&args[2]))?;
            closed.close();
            assert!(closed.refuses_execution());
            write_report(
                &args[3],
                serde_json::json!({
                    "phase": "restore", "pid": process::id(), "restored_state_verified": true,
                    "failed_capture_retired": true, "guest_failure_retired": true, "timeout_retired": true, "close_refused_execution": true,
                    "provider": "mxc-session-preview", "mxc_base": "86fb3d2abaf9c431556692037bff881830b543a5"
                }),
            )?;
        }
        _ => return Err("invalid arguments; use --help".into()),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("native state probe failed: {error}");
        process::exit(1);
    }
}
