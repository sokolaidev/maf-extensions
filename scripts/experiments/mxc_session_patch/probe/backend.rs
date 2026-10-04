//! The only file allowed to depend on the temporary MXC session API.
use hyperlight_common::session_preview::{Candidate, Error, Session, State};
use std::{path::Path, time::Duration};

#[derive(Debug)]
pub struct Failure(String);
impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}
impl std::error::Error for Failure {}
fn failure(error: Error) -> Failure {
    Failure(error.to_string())
}

pub struct BackendSession(Session);
pub struct Checkpoint(Candidate);
impl BackendSession {
    pub fn restore(path: &Path) -> Result<Self, Failure> {
        Session::restore(path).map(Self).map_err(failure)
    }
    #[cfg(feature = "bounded-output")]
    pub fn restore_bounded(path: &Path, limit: usize) -> Result<Self, Failure> {
        Session::restore_bounded(path, limit)
            .map(Self)
            .map_err(failure)
    }
    #[cfg(feature = "bounded-output")]
    pub fn take_output(&mut self) -> Result<(String, u64, bool), Failure> {
        let output = self
            .0
            .take_output()
            .ok_or_else(|| Failure("missing capture".into()))?;
        Ok((
            output.text,
            output.omitted_bytes,
            output.omitted_bytes_saturated,
        ))
    }
    pub fn execute(&mut self, code: &str) -> Result<(), Failure> {
        self.0
            .execute(code, Duration::from_secs(30))
            .map_err(failure)
    }
    pub fn capture(&mut self, path: &Path) -> Result<Checkpoint, Failure> {
        self.0
            .prepare_checkpoint(path)
            .map(Checkpoint)
            .map_err(failure)
    }
    pub fn committed(&mut self, checkpoint: &Checkpoint) -> Result<(), Failure> {
        self.0.confirm_commit(&checkpoint.0).map_err(failure)
    }
    pub fn refuses_execution(&mut self) -> bool {
        matches!(
            self.0.execute(
                "raise AssertionError('must not execute')",
                Duration::from_secs(1)
            ),
            Err(Error::InvalidState(_))
        )
    }
    pub fn rejects_stale(&mut self, checkpoint: &Checkpoint) -> bool {
        matches!(
            self.0.confirm_commit(&checkpoint.0),
            Err(Error::WrongCandidate)
        )
    }
    pub fn timeout(&mut self) -> bool {
        matches!(
            self.0
                .execute("while True: pass", Duration::from_millis(100)),
            Err(Error::TimedOut)
        ) && self.0.state() == State::RecoveryRequired
            && self.refuses_execution()
    }
    pub fn exception(&mut self) -> bool {
        matches!(
            self.0
                .execute("raise ValueError('controlled')", Duration::from_secs(1)),
            Err(Error::GuestExit(1))
        ) && self.0.state() == State::RecoveryRequired
            && self.refuses_execution()
    }
    pub fn rejects_capture(&mut self, existing: &Path) -> bool {
        matches!(
            self.0.prepare_checkpoint(existing),
            Err(Error::Checkpoint(_))
        ) && self.0.state() == State::RecoveryRequired
            && self.refuses_execution()
    }
    pub fn close(&mut self) {
        self.0.close();
    }
}

pub fn fresh_control() -> bool {
    use hyperlight_common::HyperlightScriptRunner;
    use wxc_common::{
        logger::{Logger, Mode},
        models::ExecutionRequest,
        script_runner::ScriptRunner,
    };
    let mut runner = HyperlightScriptRunner::new();
    let mut logger = Logger::new(Mode::Buffer);
    [
        "mxc_fresh_control = 17",
        "assert 'mxc_fresh_control' not in globals()",
    ]
    .iter()
    .all(|code| {
        runner
            .run(
                &ExecutionRequest {
                    script_code: (*code).into(),
                    script_timeout: 30000,
                    ..Default::default()
                },
                &mut logger,
            )
            .exit_code
            == 0
    })
}
