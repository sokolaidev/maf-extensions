
pub use mxc_sdk::hyperlight_common::session_preview::Workspace;
impl BackendSession {
    pub fn restore_files(path: &Path, workspace: std::sync::Arc<std::sync::Mutex<Workspace>>) -> Result<Self, Failure> {
        Session::restore_files(path, workspace).map(Self).map_err(failure)
    }
}
