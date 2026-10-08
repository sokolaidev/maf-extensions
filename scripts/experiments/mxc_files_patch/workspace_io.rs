//! The virtual workspace implements the kernel's hostfs byte protocol.
use std::sync::{Arc, Mutex};
use hyperlight_host::func::Registerable;
use crate::workspace::{self, Workspace, EINVAL, EPERM};
fn status(value: workspace::Result<()>) -> i32 { value.map_or_else(|e| -e, |_| 0) }
fn packed(value: workspace::Result<Vec<u8>>) -> Vec<u8> {
    match value { Ok(data) => { let mut result = 0i32.to_le_bytes().to_vec(); result.extend(data); result },
        Err(e) => (-e).to_le_bytes().to_vec() }
}
pub(crate) fn register(target: &mut impl Registerable, shared: &Arc<Mutex<Workspace>>) -> crate::Result<()> {
    { let shared = shared.clone();
        target.register_host_function("fs_mkdir", move |idx: i32, p: String| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(if idx != 0 { Err(EINVAL) } else { w.mkdir(&p) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_unlink", move |idx: i32, p: String| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(if idx != 0 { Err(EINVAL) } else { w.unlink(&p) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_truncate", move |idx: i32, p: String, length: u64| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(if idx != 0 { Err(EINVAL) } else { w.truncate(&p, length) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_rename", move |idx: i32, from: String, to: String| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(if idx != 0 { Err(EINVAL) } else { w.rename(&from, &to) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_write_bytes", move |idx: i32, p: String, offset: u64, append: i32, data: Vec<u8>| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(if idx != 0 || offset != 0 || append != 0 || !data.is_empty() { Err(EPERM) } else { w.create(&p) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("MafFsWritePart", move |token: i32, offset: u64, data: Vec<u8>| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(w.part(token, offset, &data)))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("MafFsWriteCommit", move |token: i32| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(w.commit(token)))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("MafFsWriteAbort", move |token: i32| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap(); Ok(status(w.abort(token)))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("MafFsWriteBegin", move |idx: i32, p: String, offset: u64, append: i32, length: u64| -> hyperlight_host::Result<i32> {
            let mut w = shared.lock().unwrap();
            Ok(if idx != 0 || !matches!(append, 0 | 1) { -EINVAL } else { w.begin(&p, offset, append != 0, length).unwrap_or_else(|e| -e) })
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_stat", move |idx: i32, p: String| -> hyperlight_host::Result<Vec<u8>> {
            let w = shared.lock().unwrap();
            Ok(packed(if idx != 0 { Err(EINVAL) } else { w.stat(&p).map(|(size, dir)| {
                let mut data = (size as u64).to_le_bytes().to_vec();
                data.extend((if dir { 0o40755u32 } else { 0o100666u32 }).to_le_bytes());
                data.extend([dir as u8, (!dir) as u8]); data
            }) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_read_bytes", move |idx: i32, p: String, offset: u64, size: u64| -> hyperlight_host::Result<Vec<u8>> {
            Ok(packed(if idx != 0 { Err(EINVAL) } else { shared.lock().unwrap().read(&p, offset, size) }))
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_list", move |idx: i32, p: String| -> hyperlight_host::Result<Vec<u8>> {
            let result = if idx != 0 { Err(EINVAL) } else { shared.lock().unwrap().list(&p) };
            let mut result = packed(result.and_then(|entries| {
                let mut data = (entries.len() as u32).to_le_bytes().to_vec();
                for (name, dir) in entries {
                    if data.len() + name.len() + 7 > crate::HOST_CALL_MAX { return Err(75); }
                    data.push(dir as u8); data.extend((name.len() as u16).to_le_bytes()); data.extend(name.as_bytes());
                }
                Ok(data)
            }));
            if result.len() == 4 { result.extend(0u32.to_le_bytes()); } Ok(result)
        })?;
    }
    { let shared = shared.clone();
        target.register_host_function("fs_chmod", move |idx: i32, p: String, _mode: u32| -> hyperlight_host::Result<i32> {
            Ok(status(if idx != 0 { Err(EINVAL) } else { shared.lock().unwrap().stat(&p).map(|_| ()) }))
        })?;
    }
    target.register_host_function("fs_symlink", move |_idx: i32, _p: String, _target: String| -> hyperlight_host::Result<i32> { Ok(-EPERM) })?;
    target.register_host_function("fs_link", move |_idx: i32, _p: String, _target: String| -> hyperlight_host::Result<i32> { Ok(-EPERM) })?;
    target.register_host_function("fs_readlink", move |_idx: i32, _p: String| -> hyperlight_host::Result<Vec<u8>> { Ok((-EINVAL).to_le_bytes().to_vec()) })?;
    Ok(())
}
