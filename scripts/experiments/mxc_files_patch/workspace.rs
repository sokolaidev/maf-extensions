//! Bounded virtual files; no guest path is resolved against the host filesystem.
use std::collections::{BTreeMap, BTreeSet};

pub const CHUNK: usize = 32768;
pub const EINVAL: i32 = 22;
pub const ENOENT: i32 = 2;
pub const EEXIST: i32 = 17;
pub const ENOTDIR: i32 = 20;
pub const EISDIR: i32 = 21;
pub const ENOSPC: i32 = 28;
pub const EBUSY: i32 = 16;
pub const ENOTEMPTY: i32 = 39;
pub const EPERM: i32 = 1;
pub type Result<T> = std::result::Result<T, i32>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct File { pub data: Vec<u8>, pub persistent: bool }
struct Pending { token: i32, path: String, offset: usize, length: usize, data: Vec<u8> }
pub struct Workspace {
    pub files: BTreeMap<String, File>,
    pub dirs: BTreeSet<String>,
    pub byte_limit: usize,
    pub file_limit: usize,
    pending: Option<Pending>,
    token: i32,
    faulted: bool,
}

pub fn path(path: &str, root: bool) -> Result<()> {
    if root && path.is_empty() { return Ok(()); }
    if path.is_empty() || path.len() > 551 || path.split('/').count() > 18
        || path.chars().any(|c| c.is_control() || "\\:*?\"<>|".contains(c))
        || path.split('/').any(|p| p.is_empty() || matches!(p, "." | "..") || p.ends_with(['.', ' '])) {
        return Err(EINVAL);
    }
    Ok(())
}
fn parent(path: &str) -> &str { path.rsplit_once('/').map_or("", |(p, _)| p) }

impl Workspace {
    pub fn new(bytes: usize, files: usize) -> Result<Self> {
        if bytes == 0 || bytes > 1024 * 1024 * 1024 || files == 0 || files > 1024 { return Err(EINVAL); }
        Ok(Self { files: BTreeMap::new(), dirs: BTreeSet::from([String::new()]),
            byte_limit: bytes, file_limit: files, pending: None, token: 0, faulted: false })
    }
    pub fn bytes(&self) -> usize { self.files.values().map(|f| f.data.len()).sum() }
    pub fn ready(&self) -> bool { !self.faulted && self.pending.is_none() }
    fn mutable(&self) -> Result<()> { if self.pending.is_some() { Err(EBUSY) } else { Ok(()) } }
    fn destination(&self, p: &str) -> Result<()> {
        path(p, false)?;
        if !self.dirs.contains(parent(p)) { return Err(ENOTDIR); }
        if self.dirs.contains(p) { return Err(EISDIR); }
        Ok(())
    }
    fn growth(&self, p: &str, length: usize) -> Result<()> {
        let old = self.files.get(p).map_or(0, |f| f.data.len());
        if length > self.byte_limit - (self.bytes() - old) { Err(ENOSPC) } else { Ok(()) }
    }
    pub fn mkdir(&mut self, p: &str) -> Result<()> {
        self.mutable()?; path(p, false)?;
        if self.files.contains_key(p) || self.dirs.contains(p) { return Err(EEXIST); }
        if !self.dirs.contains(parent(p)) { return Err(ENOTDIR); }
        if self.dirs.len() >= self.file_limit * 16 + 1 { return Err(ENOSPC); }
        self.dirs.insert(p.into()); Ok(())
    }
    pub fn install(&mut self, p: &str, data: Vec<u8>, persistent: bool, replace: bool) -> Result<()> {
        self.mutable()?; self.destination(p)?;
        if self.files.contains_key(p) && !replace { return Err(EEXIST); }
        if !self.files.contains_key(p) && self.files.len() >= self.file_limit { return Err(ENOSPC); }
        self.growth(p, data.len())?;
        self.files.insert(p.into(), File { data, persistent }); Ok(())
    }
    pub fn create(&mut self, p: &str) -> Result<()> {
        self.mutable()?; self.destination(p)?;
        if self.files.contains_key(p) { return Ok(()); }
        self.install(p, Vec::new(), !p.starts_with("calls/"), false)
    }
    pub fn stat(&self, p: &str) -> Result<(usize, bool)> {
        path(p, true)?;
        if self.dirs.contains(p) { return Ok((0, true)); }
        self.files.get(p).map(|f| (f.data.len(), false)).ok_or(ENOENT)
    }
    pub fn read(&self, p: &str, offset: u64, size: u64) -> Result<Vec<u8>> {
        path(p, false)?;
        if size > CHUNK as u64 { return Err(EINVAL); }
        let f = self.files.get(p).ok_or(ENOENT)?;
        let start = usize::try_from(offset).map_err(|_| EINVAL)?.min(f.data.len());
        let end = start + (size as usize).min(f.data.len() - start);
        Ok(f.data[start..end].to_vec())
    }
    pub fn list(&self, p: &str) -> Result<Vec<(String, bool)>> {
        path(p, true)?;
        if !self.dirs.contains(p) { return Err(ENOTDIR); }
        let mut result = BTreeMap::new();
        for dir in &self.dirs {
            if !dir.is_empty() && parent(dir) == p { result.insert(dir.rsplit('/').next().unwrap().into(), true); }
        }
        for file in self.files.keys() {
            if parent(file) == p { result.insert(file.rsplit('/').next().unwrap().into(), false); }
        }
        Ok(result.into_iter().collect())
    }
    pub fn truncate(&mut self, p: &str, size: u64) -> Result<()> {
        self.mutable()?; path(p, false)?;
        if !self.files.contains_key(p) { return Err(ENOENT); }
        let size = usize::try_from(size).map_err(|_| ENOSPC)?;
        self.growth(p, size)?;
        self.files.get_mut(p).unwrap().data.resize(size, 0); Ok(())
    }
    pub fn unlink(&mut self, p: &str) -> Result<()> {
        self.mutable()?; path(p, false)?;
        if self.files.remove(p).is_some() { return Ok(()); }
        if !self.dirs.contains(p) { return Err(ENOENT); }
        if !self.list(p)?.is_empty() { return Err(ENOTEMPTY); }
        self.dirs.remove(p); Ok(())
    }
    pub fn rename(&mut self, from: &str, to: &str) -> Result<()> {
        self.mutable()?; path(from, false)?; path(to, false)?;
        if from == to { self.stat(from)?; return Ok(()); }
        if !self.dirs.contains(parent(to)) { return Err(ENOTDIR); }
        if let Some(file) = self.files.get(from) {
            if self.dirs.contains(to) { return Err(EISDIR); }
            let file = file.clone(); self.files.remove(from); self.files.insert(to.into(), file); return Ok(());
        }
        if !self.dirs.contains(from) { return Err(ENOENT); }
        if to.starts_with(&format!("{from}/")) { return Err(EINVAL); }
        if self.files.contains_key(to) { return Err(ENOTDIR); }
        if self.dirs.contains(to) && !self.list(to)?.is_empty() { return Err(ENOTEMPTY); }
        let prefix = format!("{from}/");
        let renamed = |p: &str| if p == from || p.starts_with(&prefix) { format!("{to}{}", &p[from.len()..]) } else { p.into() };
        let names: Vec<_> = self.dirs.iter().chain(self.files.keys()).map(|p| renamed(p)).collect();
        for p in &names { path(p, true)?; }
        let mut dirs = BTreeSet::new();
        for p in &self.dirs { dirs.insert(renamed(p)); }
        self.dirs = dirs;
        self.files = std::mem::take(&mut self.files).into_iter().map(|(p, f)| (renamed(&p), f)).collect();
        Ok(())
    }
    pub fn begin(&mut self, p: &str, offset: u64, append: bool, length: u64) -> Result<i32> {
        self.mutable()?; self.destination(p)?;
        let file = self.files.get(p).ok_or(ENOENT)?;
        let offset = if append { file.data.len() } else { usize::try_from(offset).map_err(|_| ENOSPC)? };
        let length = usize::try_from(length).map_err(|_| ENOSPC)?;
        let end = if length == 0 { file.data.len() } else { offset.checked_add(length).ok_or(ENOSPC)? };
        self.growth(p, file.data.len().max(end))?;
        let token = self.token.checked_add(1).ok_or(EINVAL)?;
        self.token = token;
        self.pending = Some(Pending { token, path: p.into(), offset, length, data: Vec::with_capacity(length) });
        Ok(token)
    }
    fn malformed<T>(&mut self) -> Result<T> { self.pending = None; self.faulted = true; Err(EINVAL) }
    pub fn part(&mut self, token: i32, offset: u64, data: &[u8]) -> Result<()> {
        let Some(p) = self.pending.as_mut() else { return self.malformed(); };
        if token != p.token || offset != p.data.len() as u64 || data.len() > CHUNK
            || data.len() > p.length - p.data.len() { return self.malformed(); }
        p.data.extend_from_slice(data); Ok(())
    }
    pub fn commit(&mut self, token: i32) -> Result<()> {
        let Some(p) = self.pending.take() else { return self.malformed(); };
        if p.token != token || p.data.len() != p.length { return self.malformed(); }
        if p.length != 0 {
            let file = self.files.get_mut(&p.path).ok_or(ENOENT)?;
            let end = p.offset + p.length;
            file.data.resize(file.data.len().max(end), 0);
            file.data[p.offset..end].copy_from_slice(&p.data);
        }
        Ok(())
    }
    pub fn abort(&mut self, token: i32) -> Result<()> {
        if self.pending.as_ref().is_none_or(|p| p.token != token) { return self.malformed(); }
        self.pending = None; Ok(())
    }
    pub fn reclaim_calls(&mut self) -> Result<()> {
        if !self.ready() { return Err(EBUSY); }
        self.files.retain(|_, f| f.persistent);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn seeded(limit: usize) -> Workspace { let mut w = Workspace::new(limit, 2).unwrap(); w.install("f", b"old".to_vec(), false, false).unwrap(); w }
    #[test] fn whole_write_refuses_before_mutation_and_can_retry() {
        let mut w = seeded(CHUNK + 1);
        assert_eq!(w.begin("f", 0, false, (CHUNK + 2) as u64), Err(ENOSPC));
        assert_eq!(w.files["f"].data, b"old"); assert!(w.ready());
        let t = w.begin("f", 0, false, (CHUNK + 1) as u64).unwrap();
        w.part(t, 0, &vec![b'a'; CHUNK]).unwrap(); assert_eq!(w.files["f"].data, b"old");
        w.part(t, CHUNK as u64, b"b").unwrap(); w.commit(t).unwrap();
        assert_eq!(w.files["f"].data.len(), CHUNK + 1); assert!(w.ready());
    }
    #[test] fn partial_and_forged_frames_never_publish() {
        for forged in [false, true] {
            let mut w = seeded(10); let t = w.begin("f", 0, false, 5).unwrap();
            w.part(t, 0, b"ab").unwrap();
            if forged { assert_eq!(w.part(t + 1, 2, b"cde"), Err(EINVAL)); }
            else { assert_eq!(w.commit(t), Err(EINVAL)); }
            assert_eq!(w.files["f"].data, b"old"); assert!(!w.ready());
        }
    }
    #[test] fn truncate_append_sparse_and_count_are_bounded() {
        let mut w = seeded(5); assert_eq!(w.truncate("f", 6), Err(ENOSPC));
        assert_eq!(w.begin("f", u64::MAX, false, 1), Err(ENOSPC));
        let t = w.begin("f", 0, true, 2).unwrap(); w.part(t, 0, b"++").unwrap(); w.commit(t).unwrap();
        assert_eq!(w.files["f"].data, b"old++");
        w.truncate("f", 1).unwrap(); w.install("g", b"data".to_vec(), true, false).unwrap();
        assert_eq!(w.create("h"), Err(ENOSPC)); assert_eq!(w.bytes(), 5);
    }
    #[test] fn links_and_paths_have_no_host_authority() {
        for p in ["../x", "/x", "a/../b", "a\\b", "C:x", "a//b"] { assert!(path(p, false).is_err()); }
        let mut w = seeded(10); w.mkdir("dir").unwrap(); w.rename("f", "dir/f").unwrap();
        w.reclaim_calls().unwrap(); assert!(w.files.is_empty());
    }
    #[test] fn mutations_cannot_race_staged_write() {
        let mut w = seeded(10); let t = w.begin("f", 0, false, 2).unwrap();
        assert_eq!(w.unlink("f"), Err(EBUSY)); assert_eq!(w.truncate("f", 0), Err(EBUSY));
        assert_eq!(w.rename("f", "g"), Err(EBUSY)); w.abort(t).unwrap(); assert!(w.ready());
    }
}
