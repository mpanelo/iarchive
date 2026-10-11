//! Mount and root bindings, separate from content verification.
use anyhow::{Result, ensure};
use iarchive_core::is_link;
use serde::{Deserialize, Serialize};
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VolumeIdentity {
    pub volume_id: String,
    pub root_id: String,
    /// Windows disk numbers are current OS topology, not permanent hardware IDs.
    pub physical_disks: Option<Vec<u32>>,
}

pub fn checked_directory(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    ensure!(
        absolute.is_absolute(),
        "drive-relative paths are not supported"
    );
    let mut current = PathBuf::new();
    for part in absolute.components() {
        ensure!(
            !matches!(part, Component::ParentDir),
            "path traversal refused"
        );
        current.push(part);
        if matches!(part, Component::Prefix(_)) {
            continue;
        }
        let metadata = std::fs::symlink_metadata(&current)?;
        ensure!(
            metadata.is_dir() && !is_link(&metadata),
            "directory is unavailable or linked: {}",
            current.display()
        );
    }
    Ok(absolute.canonicalize()?)
}

pub fn identity(root: &Path) -> Result<VolumeIdentity> {
    checked_directory(root)?;
    native_identity(root)
}

pub fn independent(a: &VolumeIdentity, b: &VolumeIdentity) -> bool {
    a.volume_id != b.volume_id
        && match (&a.physical_disks, &b.physical_disks) {
            (Some(a), Some(b)) => {
                !a.is_empty() && !b.is_empty() && a.iter().all(|disk| !b.contains(disk))
            }
            _ => false,
        }
}

#[cfg(unix)]
fn native_identity(root: &Path) -> Result<VolumeIdentity> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::symlink_metadata(root)?;
    Ok(VolumeIdentity {
        volume_id: format!("unix-device:{}", metadata.dev()),
        root_id: format!("inode:{}", metadata.ino()),
        physical_disks: None,
    })
}

#[cfg(windows)]
fn native_identity(root: &Path) -> Result<VolumeIdentity> {
    use std::{
        ffi::c_void,
        os::windows::{
            ffi::OsStrExt,
            io::{AsRawHandle, FromRawHandle},
        },
    };
    type Handle = *mut c_void;
    #[repr(C)]
    #[derive(Default)]
    struct FileInfo {
        attributes: u32,
        created: [u32; 2],
        accessed: [u32; 2],
        modified: [u32; 2],
        volume: u32,
        size_high: u32,
        size_low: u32,
        links: u32,
        index_high: u32,
        index_low: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Extent {
        disk: u32,
        start: i64,
        length: i64,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Extents {
        count: u32,
        extents: [Extent; 1],
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetVolumePathNameW(path: *const u16, mount: *mut u16, length: u32) -> i32;
        fn GetVolumeNameForVolumeMountPointW(
            mount: *const u16,
            volume: *mut u16,
            length: u32,
        ) -> i32;
        fn CreateFileW(
            path: *const u16,
            access: u32,
            share: u32,
            security: *const c_void,
            disposition: u32,
            flags: u32,
            template: Handle,
        ) -> Handle;
        fn GetFileInformationByHandle(handle: Handle, info: *mut FileInfo) -> i32;
        fn DeviceIoControl(
            handle: Handle,
            code: u32,
            input: *const c_void,
            input_size: u32,
            output: *mut c_void,
            output_size: u32,
            returned: *mut u32,
            overlapped: *mut c_void,
        ) -> i32;
    }
    fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
        value.encode_wide().chain(Some(0)).collect()
    }
    let path = wide(root.as_os_str());
    let mut mount = [0u16; 32768];
    let mut volume = [0u16; 128];
    // Buffers live across each synchronous API call; all handles are wrapped in File for RAII.
    unsafe {
        ensure!(
            GetVolumePathNameW(path.as_ptr(), mount.as_mut_ptr(), mount.len() as u32) != 0,
            "cannot resolve Windows volume: {}",
            std::io::Error::last_os_error()
        );
        ensure!(
            GetVolumeNameForVolumeMountPointW(
                mount.as_ptr(),
                volume.as_mut_ptr(),
                volume.len() as u32
            ) != 0,
            "cannot bind Windows volume identity: {}",
            std::io::Error::last_os_error()
        );
        let len = volume
            .iter()
            .position(|value| *value == 0)
            .unwrap_or(volume.len());
        let volume_id = String::from_utf16(&volume[..len])?;
        let handle = CreateFileW(
            path.as_ptr(),
            0,
            7,
            std::ptr::null(),
            3,
            0x02000000 | 0x00200000,
            std::ptr::null_mut(),
        ); // backup semantics, open reparse point
        ensure!(
            handle as isize != -1,
            "cannot open root identity: {}",
            std::io::Error::last_os_error()
        );
        let file = std::fs::File::from_raw_handle(handle);
        let mut info = FileInfo::default();
        ensure!(
            GetFileInformationByHandle(file.as_raw_handle(), &mut info) != 0
                && info.attributes & 0x400 == 0,
            "cannot bind non-linked Windows root"
        );
        let root_id = format!(
            "file-index:{}:{}:{}",
            info.volume, info.index_high, info.index_low
        );
        let device = wide(std::ffi::OsStr::new(volume_id.trim_end_matches('\\')));
        let handle = CreateFileW(
            device.as_ptr(),
            0,
            7,
            std::ptr::null(),
            3,
            0,
            std::ptr::null_mut(),
        );
        let physical_disks = if handle as isize == -1 {
            None
        } else {
            let device = std::fs::File::from_raw_handle(handle);
            let mut extents = Extents::default();
            let mut returned = 0;
            let ok = DeviceIoControl(
                device.as_raw_handle(),
                0x00560000,
                std::ptr::null(),
                0,
                (&mut extents as *mut Extents).cast(),
                std::mem::size_of::<Extents>() as u32,
                &mut returned,
                std::ptr::null_mut(),
            );
            let query = [0u32; 3]; // StorageDeviceProperty, PropertyStandardQuery
            let mut descriptor = [0u8; 4096];
            let mut descriptor_bytes = 0;
            let described = DeviceIoControl(
                device.as_raw_handle(),
                0x002d1400,
                query.as_ptr().cast(),
                std::mem::size_of_val(&query) as u32,
                descriptor.as_mut_ptr().cast(),
                descriptor.len() as u32,
                &mut descriptor_bytes,
                std::ptr::null_mut(),
            );
            let bus = u32::from_le_bytes(descriptor[28..32].try_into().unwrap());
            // Accept only driver-reported ATA, USB, SATA or NVMe backing. Virtual,
            // pooled, network and unknown bus types retain NOT VERIFIED.
            if ok != 0
                && extents.count == 1
                && returned >= std::mem::size_of::<Extents>() as u32
                && described != 0
                && descriptor_bytes >= 36
                && matches!(bus, 3 | 7 | 11 | 17)
            {
                Some(vec![extents.extents[0].disk])
            } else {
                None
            }
        };
        Ok(VolumeIdentity {
            volume_id,
            root_id,
            physical_disks,
        })
    }
}
