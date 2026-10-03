//! A private pipe binds native execution to the lifetime of its host owner.
use std::{io::Read, process, thread};

const HELLO: &[u8; 8] = b"MXCOWN1\n";

pub fn watch() {
    let mut header = [0; 8];
    if std::io::stdin().read_exact(&mut header).is_err() {
        process::exit(74);
    }
    if &header != HELLO {
        process::exit(75);
    }
    thread::spawn(|| {
        let mut command = [0; 1];
        let status = match std::io::stdin().read(&mut command) {
            Ok(0) | Err(_) => 74,
            Ok(_) if command[0] == b'C' => 74,
            Ok(_) => 75,
        };
        // Termination closes the VM handles even if guest execution cannot unwind.
        process::exit(status);
    });
}

#[cfg(test)]
mod tests {
    use std::{
        io::{BufRead, BufReader, Write},
        process::{Command, Stdio},
        thread,
        time::{Duration, Instant},
    };

    #[test]
    fn owner_child() {
        if std::env::var_os("MXC_TEST_OWNER").is_some() {
            super::watch();
            println!("owner-ready");
            loop {
                thread::park();
            }
        }
    }

    #[test]
    fn private_pipe_controls_process_lifetime() {
        for (header, command, expected) in [
            (&b""[..], None, 74),
            (&b"INVALID\n"[..], None, 75),
            (&b"MXCOWN1\n"[..], None, 74),
            (&b"MXCOWN1\n"[..], Some(b'C'), 74),
            (&b"MXCOWN1\n"[..], Some(b'X'), 75),
        ] {
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "owner::tests::owner_child", "--nocapture"])
                .env("MXC_TEST_OWNER", "1")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            let mut input = child.stdin.take().unwrap();
            input.write_all(header).unwrap();
            input.flush().unwrap();
            if header == super::HELLO {
                let mut output = BufReader::new(child.stdout.take().unwrap());
                let mut line = String::new();
                loop {
                    assert_ne!(output.read_line(&mut line).unwrap(), 0);
                    if line.contains("owner-ready") {
                        break;
                    }
                    line.clear();
                }
                assert!(child.try_wait().unwrap().is_none());
            }
            if let Some(byte) = command {
                input.write_all(&[byte]).unwrap();
                input.flush().unwrap();
            } else {
                drop(input);
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                if let Some(status) = child.try_wait().unwrap() {
                    assert_eq!(status.code(), Some(expected));
                    break;
                }
                if Instant::now() >= deadline {
                    child.kill().unwrap();
                    child.wait().unwrap();
                    panic!("owner watchdog did not terminate");
                }
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
