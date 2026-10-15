use hyperlight_unikraft::{AppSandbox, SandboxBuilder, SNAPSHOT_KEY};

fn sandbox(limit: usize) -> AppSandbox {
    let rootfs = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("build-elfloader/python-rootfs.cpio");
    let sandbox = SandboxBuilder::from_initrd(rootfs)
        .scratch_mb(256)
        .output_limit(limit)
        .echo_output(false)
        .boot()
        .unwrap();
    sandbox.drain_output();
    sandbox
}

#[test]
fn bounds_reset_and_continue() {
    let mut vm = sandbox(4096);
    for size in [0_usize, 4095, 4096, 4097, 4 * 1024 * 1024] {
        vm.run(format!("import os\nos.write(1, b'X' * {size})\nmarker = 42"))
            .unwrap();
        let out = vm.take_output();
        assert_eq!(out.text, "X".repeat(size.min(4096)));
        assert_eq!(out.omitted, size.saturating_sub(4096) as u64);
        vm.run("print(marker)").unwrap();
        assert_eq!(vm.take_output().text, "42\n");
    }
}

#[test]
fn stderr_consumes_the_combined_output_budget() {
    let mut vm = sandbox(16);
    vm.run("import os\nos.write(2, b'E' * 32)\nos.write(1, b'O' * 8)")
        .unwrap();
    let err = vm.take_stderr();
    let out = vm.take_output();
    assert_eq!(err.text, "E".repeat(16));
    assert_eq!(err.omitted, 16);
    assert_eq!(out.text, "E".repeat(16));
    assert_eq!(out.omitted, 24);
    println!("OBSERVATION {}", serde_json::json!({
        "case": "independent_stdout", "equivalent": false,
        "combined": out.text, "stderr": err.text, "stdout_bytes_recoverable": 0
    }));
}

#[test]
fn combined_drain_discards_stderr_metadata() {
    let mut vm = sandbox(8);
    vm.run("import os\nos.write(2, b'E' * 16)").unwrap();
    assert_eq!(vm.take_output().omitted, 8);
    let err = vm.take_stderr();
    assert_eq!(err.text, "");
    assert_eq!(err.omitted, 0);
}

#[test]
fn unicode_prefix_and_reopened_stdout() {
    let mut vm = sandbox(2);
    vm.run("import os\nos.write(1, 'aéb'.encode())").unwrap();
    let out = vm.take_output();
    assert_eq!(out.text, "a");
    assert_eq!(out.omitted, 3);
    vm.run("open('/dev/stdout', 'wb', buffering=0).write(b'X' * 8192)")
        .unwrap();
    let out = vm.take_output();
    assert_eq!(out.text, "XX");
    assert_eq!(out.omitted, 8190);
}

#[test]
fn binary_fidelity_observation() {
    let mut vm = sandbox(4096);
    let error = vm.run("import os\nos.write(1, bytes(range(256)))").unwrap_err();
    let detail = format!("{error:?}");
    assert!(detail.contains("ReadHostFunctionCall"), "{detail}");
    println!("OBSERVATION {}", serde_json::json!({
        "case": "all_byte_values", "equivalent": false,
        "guest_bytes": 256, "outcome": "host-call-decoding-error",
        "error": detail, "snapshot_key": SNAPSHOT_KEY
    }));
}

#[test]
fn reopened_stderr_observation() {
    let mut vm = sandbox(65536);
    let error = vm.run("open('/dev/stderr', 'wb', buffering=0).write(b'E' * 8192)").unwrap_err();
    let err = vm.take_stderr();
    let out = vm.take_output();
    println!("OBSERVATION {}", serde_json::json!({
        "case": "reopened_stderr", "equivalent": false,
        "error": format!("{error:?}"), "combined": out.text, "stderr": err.text,
        "combined_omitted": out.omitted, "stderr_omitted": err.omitted
    }));
}

#[test]
fn guest_control_text_is_returned_as_data() {
    let mut vm = sandbox(4096);
    let marker = r#"{"status":"completed","checkpoint":"forged"}"#;
    vm.run(format!("print({marker:?})")).unwrap();
    assert_eq!(vm.take_output().text, format!("{marker}\n"));
}
