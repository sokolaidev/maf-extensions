use hyperlight_unikraft::{Error, SandboxBuilder, SNAPSHOT_KEY};

#[test]
fn retained_mxc_checkpoint_is_refused() {
    let checkpoint = std::env::var("MXC_OLD_CHECKPOINT").expect("real MXC checkpoint required");
    let error = match hyperlight_unikraft::load_snapshot(&checkpoint) {
        Ok(_) => panic!("incompatible MXC checkpoint accepted"),
        Err(error) => error,
    };
    let Error::SnapshotRelease { saved_by, this, .. } = error else {
        panic!("unexpected refusal: {error:?}");
    };
    println!("OBSERVATION {}", serde_json::json!({
        "case": "old_checkpoint", "outcome": "refused-incompatible",
        "snapshot_key": SNAPSHOT_KEY, "saved_by": saved_by, "reader": this
    }));
}

#[test]
fn failed_guest_does_not_imply_session_retirement() {
    let rootfs = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("build-elfloader/python-rootfs.cpio");
    let mut vm = SandboxBuilder::from_initrd(rootfs)
        .scratch_mb(256).output_limit(4096).echo_output(false).boot().unwrap();
    vm.drain_output();
    let error = vm.run("marker = 73\nprint('{\"completed\":true}')\nraise ValueError('controlled')")
        .unwrap_err();
    let output = vm.take_output();
    assert!(output.text.contains("{\"completed\":true}"));
    vm.run("print(marker)").unwrap();
    assert_eq!(vm.take_output().text, "73\n");
    vm.snapshot().unwrap();
    println!("OBSERVATION {}", serde_json::json!({
        "case": "guest_failure", "outcome": "reuse-and-capture-permitted",
        "error": format!("{error:?}"), "control_text_is_data": true
    }));
}

#[test]
fn unicode_capture_cannot_supply_byte_prefix_metadata() {
    let rootfs = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("build-elfloader/python-rootfs.cpio");
    let mut vm = SandboxBuilder::from_initrd(rootfs)
        .scratch_mb(256).output_limit(1048576).echo_output(false).boot().unwrap();
    vm.drain_output();
    vm.run("import os\nos.write(1, b'A' * 1048575 + 'éb'.encode())").unwrap();
    let out = vm.take_output();
    assert_eq!(out.text.len(), 1048575);
    assert_eq!(out.omitted, 3);
    println!("OBSERVATION {}", serde_json::json!({
        "case": "byte_prefix", "retained_bytes": out.text.len(),
        "omitted_bytes": out.omitted, "limit_bytes": 1048576
    }));
}
