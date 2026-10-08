
impl BackendSession {
    pub fn take_streams(&mut self) -> Result<(Vec<u8>, Vec<u8>, serde_json::Value), Failure> {
        let output = self.0.take_streams().ok_or_else(|| Failure("missing streams".into()))?;
        if output.faulted { return Err(Failure("stream transport fault".into())); }
        let metadata = serde_json::json!({
            "stdout": {"retained_bytes": output.stdout.bytes.len(), "omitted_bytes": output.stdout.omitted_bytes,
                "omitted_bytes_saturated": output.stdout.omitted_bytes_saturated},
            "stderr": {"retained_bytes": output.stderr.bytes.len(), "omitted_bytes": output.stderr.omitted_bytes,
                "omitted_bytes_saturated": output.stderr.omitted_bytes_saturated}
        });
        Ok((output.stdout.bytes, output.stderr.bytes, metadata))
    }
}
