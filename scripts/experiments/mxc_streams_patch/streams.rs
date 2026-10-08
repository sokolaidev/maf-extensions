//! Native control is published only after byte capture and optional checkpoint export.
use crate::{backend::BackendSession, owner};
use std::{error::Error, fs::{self, OpenOptions}, io::Write, path::Path};

fn write_new(path: &Path, data: &[u8]) -> Result<(), Box<dyn Error>> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(data)?;
    Ok(())
}

pub fn run(args: &[String]) -> Result<bool, Box<dyn Error>> {
    let mode = args.get(1).map(String::as_str);
    if !matches!(mode, Some("call-streams" | "execute-streams")) { return Ok(false); }
    if args.len() != 8 { return Err("invalid stream arguments".into()); }
    owner::watch();
    if fs::metadata(&args[4])?.len() > 65536 { return Err("code exceeds probe limit".into()); }
    let code = fs::read_to_string(&args[4])?;
    let mut sandbox = BackendSession::restore_bounded(Path::new(&args[2]), 65536)?;
    sandbox.execute(&code)?;
    assert!(sandbox.refuses_execution());
    let (stdout, stderr, metadata) = sandbox.take_streams()?;
    let checkpoint = mode == Some("call-streams");
    if checkpoint {
        sandbox.capture_bounded(Path::new(&args[3]), args[6].parse()?, args[7].parse()?)?;
    }
    sandbox.close();
    let report = Path::new(&args[5]);
    write_new(&report.with_extension("stdout.bin"), &stdout)?;
    write_new(&report.with_extension("stderr.bin"), &stderr)?;
    let control = serde_json::to_vec(&serde_json::json!({
        "format": "mxc-byte-streams-v1", "completed": true, "checkpoint": checkpoint,
        "limit_bytes": 1048576, "streams": metadata
    }))?;
    if control.len() > 1024 { return Err("control report exceeds limit".into()); }
    let staged = report.with_extension("part");
    write_new(&staged, &control)?;
    if report.exists() { return Err("control destination already exists".into()); }
    fs::rename(staged, report)?;
    Ok(true)
}
