//! The helper owns completion and captures files only after guest execution stops.
use crate::{backend::{BackendSession, Workspace}, owner};
use serde_json::{Value, json};
use std::{error::Error, fs::{self, OpenOptions}, io::{Read, Write}, path::Path, sync::{Arc, Mutex}};
type Result<T> = std::result::Result<T, Box<dyn Error>>;
const META: usize = 16 * 1024 * 1024;
fn number(value: &Value, name: &str, max: usize) -> Result<usize> {
    let n = value[name].as_u64().ok_or("invalid numeric field")?;
    if n > max as u64 { return Err("numeric field exceeds bound".into()); }
    Ok(n as usize)
}
fn text<'a>(value: &'a Value, name: &str) -> Result<&'a str> { value[name].as_str().ok_or_else(|| "invalid text field".into()) }
fn bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let mut data = Vec::new(); fs::File::open(path)?.take(limit as u64 + 1).read_to_end(&mut data)?;
    if data.len() > limit { return Err("file exceeds bound".into()); } Ok(data)
}
fn write(path: &Path, bytes: &[u8]) -> Result<()> { OpenOptions::new().write(true).create_new(true).open(path)?.write_all(bytes)?; Ok(()) }
fn check<T>(r: std::result::Result<T, i32>) -> Result<T> { r.map_err(|e| format!("workspace errno {e}").into()) }
fn parents(w: &mut Workspace, p: &str) -> Result<()> {
    let parts: Vec<_> = p.split('/').collect();
    for i in 1..parts.len() { let p = parts[..i].join("/"); if !w.dirs.contains(&p) { check(w.mkdir(&p))?; } }
    Ok(())
}
fn load(w: &mut Workspace, base: &Path) -> Result<()> {
    let meta: Value = serde_json::from_slice(&bounded(&base.join("workspace.json"), META)?)?;
    if meta["format"] != "mxc-workspace-v1" { return Err("workspace format differs".into()); }
    let data = bounded(&base.join("workspace.bin"), w.byte_limit)?;
    let dirs = meta["dirs"].as_array().ok_or("invalid directories")?;
    if dirs.len() > w.file_limit * 18 + 3 { return Err("directory count exceeds bound".into()); }
    for dir in dirs { check(w.mkdir(dir.as_str().ok_or("invalid directory")?))?; }
    let entries = meta["files"].as_array().ok_or("invalid workspace inventory")?;
    if entries.len() > w.file_limit { return Err("file count exceeds bound".into()); }
    let mut offset = 0;
    for entry in entries {
        let len = number(entry, "bytes", w.byte_limit)?;
        if number(entry, "offset", w.byte_limit)? != offset || len > data.len() - offset { return Err("invalid workspace range".into()); }
        let p = text(entry, "name")?;
        check(w.install(p, data[offset..offset + len].to_vec(), true, false))?; offset += len;
    }
    if offset != data.len() { return Err("unreferenced workspace bytes".into()); } Ok(())
}
fn state(w: &Workspace) -> Result<(Vec<u8>, Vec<u8>)> {
    let mut data = Vec::new(); let mut files = Vec::new();
    for (name, file) in &w.files { files.push(json!({"name":name,"offset":data.len(),"bytes":file.data.len()})); data.extend_from_slice(&file.data); }
    let dirs: Vec<_> = w.dirs.iter().filter(|p| !p.is_empty() && (!(p.as_str() == "calls" || p.starts_with("calls/")) || w.files.keys().any(|f| f.starts_with(&format!("{p}/"))))).collect();
    let meta = serde_json::to_vec(&json!({"format":"mxc-workspace-v1","dirs":dirs,"files":files}))?;
    if meta.len() > META { return Err("workspace metadata exceeds bound".into()); } Ok((meta, data))
}
pub fn run(args: &[String]) -> Result<bool> {
    if !matches!(args.get(1).map(String::as_str), Some("call-files" | "execute-files")) { return Ok(false); }
    if args.len() != 8 { return Err("invalid file arguments".into()); }
    owner::watch();
    let work = Path::new(&args[4]).parent().ok_or("request has no directory")?;
    let request: Value = serde_json::from_slice(&bounded(&work.join("request.json"), META)?)?;
    if request["uploads_valid"].as_bool() != Some(true) { return Err("upload conflicts with retained namespace".into()); }
    let token = text(&request,"token")?;
    if token.len() != 32 || !token.bytes().all(|c| c.is_ascii_hexdigit()) { return Err("invalid call token".into()); }
    let byte_limit = number(&request["workspace"],"bytes",1024*1024*1024)?;
    let file_limit = number(&request["workspace"],"files",1024)?;
    let mut workspace = check(Workspace::new(byte_limit,file_limit))?;
    match request["restoring"].as_bool().ok_or("missing restore mode")? {
        true => load(&mut workspace,Path::new(&args[2]))?,
        false => { if Path::new(&args[2]).join("workspace.json").exists() { return Err("unrequested file restore".into()); } }
    }
    let input_limit = number(&request["limits"],"input_bytes",64*1024*1024)?;
    let inputs = bounded(&work.join("inputs.bin"),input_limit)?;
    let items = request["inputs"].as_array().ok_or("invalid inputs")?;
    if items.len() > number(&request["limits"],"input_files",1024)? { return Err("input count exceeded".into()); }
    let per_file = number(&request["limits"],"file_bytes",64*1024*1024)?;
    let call_root = format!("calls/{token}");
    parents(&mut workspace,&format!("{call_root}/_"))?;
    if !workspace.dirs.contains("session") { check(workspace.mkdir("session"))?; }
    let mut offset = 0;
    for item in items {
        let name = text(item,"name")?;
        let len = number(item,"bytes",per_file)?;
        if number(item,"offset",input_limit)? != offset || len > inputs.len()-offset { return Err("invalid input range".into()); }
        let persistent = match text(item,"lifecycle")? { "session" => true, "call" => false, _ => return Err("invalid lifecycle".into()) };
        let replace = item["replace"].as_bool().ok_or("invalid replacement authority")?;
        let session_path = format!("session/{name}");
        if workspace.files.contains_key(&session_path) {
            if !replace { return Err("input replacement not authorized".into()); }
            check(workspace.unlink(&session_path))?;
        }
        let p = if persistent { session_path } else { format!("{call_root}/{name}") };
        parents(&mut workspace,&p)?;
        check(workspace.install(&p,inputs[offset..offset+len].to_vec(),persistent,false))?; offset += len;
    }
    if offset != inputs.len() { return Err("unreferenced input bytes".into()); }
    let shared = Arc::new(Mutex::new(workspace));
    let mut sandbox = BackendSession::restore_files(Path::new(&args[2]), shared.clone())?;
    let mut code = format!("import os\nguest_call_path={}\nguest_session_path='/workspace/session'\nos.chdir(guest_call_path)\n", serde_json::to_string(&format!("/workspace/{call_root}"))?);
    code.push_str(&String::from_utf8(bounded(Path::new(&args[4]),65536)?)?);
    write(&Path::new(&args[5]).with_extension("ready"), b"ready\n")?;
    let execution = sandbox.execute(&code);
    let (stdout,stderr,streams) = sandbox.take_streams()?;
    let report = Path::new(&args[5]);
    write(&report.with_extension("stdout.bin"),&stdout)?;
    write(&report.with_extension("stderr.bin"),&stderr)?;
    execution?;
    let mut w = shared.lock().unwrap();
    if !w.ready() { return Err("incomplete or malformed file transfer".into()); }
    let selected = request["artifacts"].as_array().ok_or("invalid artifacts")?;
    if selected.len() > number(&request["limits"],"artifact_files",1024)? { return Err("artifact count exceeded".into()); }
    let artifact_limit = number(&request["limits"],"artifact_bytes",64*1024*1024)?;
    let mut artifacts = Vec::new(); let mut payload = Vec::new();
    for name in selected {
        let name = name.as_str().ok_or("invalid artifact name")?;
        let p = format!("{call_root}/{name}");
        let file = w.files.get(&p).ok_or("requested artifact missing or unsafe")?;
        if file.data.len() > per_file || file.data.len() > artifact_limit-payload.len() { return Err("artifact allowance exceeded".into()); }
        artifacts.push(json!({"name":name,"offset":payload.len(),"bytes":file.data.len()})); payload.extend_from_slice(&file.data);
    }
    check(w.reclaim_calls())?;
    let (manifest,bytes) = state(&w)?;
    let retained_files = w.files.len(); let retained_bytes = w.bytes();
    drop(w);
    let checkpoint = args[1] == "call-files";
    if checkpoint {
        let max_bytes: u64 = args[6].parse()?; let max_files: u64 = args[7].parse()?;
        let allowance = max_bytes.checked_sub((manifest.len()+bytes.len()) as u64).ok_or("checkpoint cannot cover workspace")?;
        let count = max_files.checked_sub(2).ok_or("checkpoint cannot cover workspace files")?;
        sandbox.capture_bounded(Path::new(&args[3]), allowance, count)?;
        write(&Path::new(&args[3]).join("workspace.json"),&manifest)?;
        write(&Path::new(&args[3]).join("workspace.bin"),&bytes)?;
    }
    sandbox.close();
    write(&report.with_extension("artifacts.bin"),&payload)?;
    let control = serde_json::to_vec(&json!({"format":"mxc-files-result-v1","completed":true,"checkpoint":checkpoint,
        "limit_bytes":1048576,"streams":streams,"artifacts":artifacts,"workspace_bytes":retained_bytes,"workspace_files":retained_files}))?;
    if control.len() > META { return Err("completion metadata exceeds limit".into()); }
    write(&report.with_extension("part"),&control)?;
    if report.exists() { return Err("completion destination exists".into()); }
    fs::rename(report.with_extension("part"),report)?; Ok(true)
}
