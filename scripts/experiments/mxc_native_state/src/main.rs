use hyperlight_unikraft::SandboxBuilder;
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
            "mxc-native-state-spike seed <initrd> <new-checkpoint> <report> [hold]\nmxc-native-state-spike restore <checkpoint> <report>"
        );
        return Ok(());
    }
    match args.get(1).map(String::as_str) {
        Some("seed") if args.len() == 5 || (args.len() == 6 && args[5] == "hold") => {
            let checkpoint = Path::new(&args[3]);
            if checkpoint.exists() {
                return Err("checkpoint destination must not exist".into());
            }
            let mut sandbox = SandboxBuilder::from_initrd(&args[2])
                .scratch_mb(1536)
                .boot()?;
            sandbox.run(SEED)?;
            sandbox.run("assert mxc_value == 73\nmxc_frame.loc[0, 'x'] = 4")?;
            sandbox.run(VERIFY)?;
            sandbox.snapshot_to(checkpoint)?;
            // Distinguish saved state from mutations made after capture.
            sandbox.run("mxc_value = 999\nmxc_frame.loc[0, 'x'] = 999")?;
            write_report(
                &args[4],
                serde_json::json!({
                    "phase": "seed", "pid": process::id(), "continuity": true,
                    "snapshot_saved": true, "post_snapshot_mutation": true,
                    "hyperlight_unikraft": "0.14.1"
                }),
            )?;
            if args.len() == 6 {
                loop {
                    thread::sleep(Duration::from_secs(1));
                }
            }
        }
        Some("restore") if args.len() == 4 => {
            let mut sandbox = SandboxBuilder::from_snapshot_dir(&args[2])?.boot()?;
            sandbox.run(VERIFY)?;
            write_report(
                &args[3],
                serde_json::json!({
                    "phase": "restore", "pid": process::id(), "restored_state_verified": true,
                    "hyperlight_unikraft": "0.14.1"
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
