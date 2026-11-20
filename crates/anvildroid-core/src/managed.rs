//! Desktop actions pinned to an owner-authorized controller runtime.
use crate::{
    apps, backend,
    controller::{self, Request},
    error::AnvilError,
    library,
};
use serde_json::Value;
use std::time::{Duration, Instant};

pub fn validate(id: &str, package: &str) -> Result<(), AnvilError> {
    if !library::valid_managed_runtime(id) || !backend::valid_package(package) {
        return Err(AnvilError::InvalidArgument(
            "Invalid runtime or package".into(),
        ));
    }
    Ok(())
}

pub fn launch(id: &str, package: &str) -> Result<(), AnvilError> {
    validate(id, package)?;
    launch_with(id, package, |request| {
        if matches!(&request, Request::AppAction { action, .. } if action == "launch" || action == "full_ui") {
            let exe=std::env::current_exe().map_err(|e| e.to_string())?;
            crate::desktop::register_managed_window(id, package, &exe).map_err(|e| e.to_string())?;
        }
        controller::request(request)
    }, || {
        std::thread::sleep(Duration::from_millis(500))
    })
    .map_err(AnvilError::BackendFailed)?;
    let _ = library::launched_runtime(id, package);
    Ok(())
}

fn launch_with(
    id: &str,
    package: &str,
    mut request: impl FnMut(Request) -> Result<Value, String>,
    mut wait: impl FnMut(),
) -> Result<(), String> {
    let display = request(Request::DisplayInfo { id: id.into() })?;
    if display["mode"] != "desktop"
        || display["effective_mode"]
            .as_str()
            .is_some_and(|mode| mode != "desktop")
    {
        return Err(
            "Desktop integration is unavailable. Restart this runtime from your graphical session before opening its shortcut.".into(),
        );
    }
    let deadline = Instant::now() + Duration::from_secs(360);
    let mut started = false;
    loop {
        let record = request(Request::Refresh { id: id.into() })?;
        match record["state"].as_str() {
            Some("Running" | "Frozen") => break,
            Some("Prepared" | "Stopped") if !started => {
                request(Request::Start {
                    id: id.into(),
                    display: controller::desktop_session_display(),
                })?;
                started = true;
            }
            Some("Starting") => {}
            _ => return Err(
                "Runtime is not ready. Review its status in Settings before opening this shortcut."
                    .into(),
            ),
        }
        if Instant::now() >= deadline {
            return Err("Timed out waiting for this runtime to start.".into());
        }
        wait();
    }
    let action = if package == "org.anvildroid.desktop" { "full_ui" } else { "launch" };
    let mut job = request(Request::AppAction {
        id: id.into(),
        action: action.into(),
        package: package.into(),
        activation_token: None,
        user: None,
        name: None,
    })?;
    let job_id = job["id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("Missing app job ID")?
        .to_owned();
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        if job["id"] != job_id
            || job["runtime_id"] != id
            || job["package"] != package
            || job["action"] != action
        {
            return Err("App job changed; review this runtime in Settings.".into());
        }
        match job["status"].as_str() {
            Some("Succeeded") => return Ok(()),
            Some("Running") => {}
            Some("Failed") => {
                return Err(job["error"].as_str().unwrap_or("App launch failed").into())
            }
            _ => return Err("Invalid app job status".into()),
        }
        if Instant::now() >= deadline {
            return Err("Timed out waiting for app launch.".into());
        }
        wait();
        job = request(Request::AppJob { id: id.into() })?;
    }
}

pub fn install(id: &str, path: &std::path::Path) -> Result<(), AnvilError> {
    install_through_restart(|| {
        let job = controller::install(id, path).map_err(AnvilError::BackendFailed)?;
        await_install(id, job)
    })
}

/// Submit an install again when it failed only because Android's system
/// process restarted under it. A result that is unknown is never repeated:
/// see `apps::retry_after_restart`.
fn install_through_restart(submit: impl FnMut() -> Result<(), AnvilError>) -> Result<(), AnvilError> {
    let deadline = Instant::now() + apps::RESTART_WAIT;
    apps::retry_after_restart(submit, || {
        std::thread::sleep(apps::RESTART_PAUSE);
        Instant::now() < deadline
    })
}

/// Install the APKs of one package as one Android session. The controller
/// receives one caller-opened file and the size of each APK in it.
pub fn install_set(id: &str, set: &crate::apk_set::PackageSet) -> Result<(), AnvilError> {
    if backend::uninstall_block_reason(&set.package).is_some() {
        return Err(AnvilError::InvalidArgument(
            "Runtime components must be updated through the runtime installer.".into(),
        ));
    }
    let (file, sizes) = set.transfer_file()?;
    // The controller reads the file by position, so it can be sent again.
    install_through_restart(|| {
        let job = controller::install_parts(id, &file, &sizes).map_err(AnvilError::BackendFailed)?;
        await_install(id, job)
    })
}

/// Wait for the exact accepted install job, without following a later action.
fn await_install(id: &str, mut job: Value) -> Result<(), AnvilError> {
    let job_id = job["id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AnvilError::BackendFailed("Missing installation job ID".into()))?
        .to_owned();
    let deadline = Instant::now() + Duration::from_secs(360);
    loop {
        if job["id"] != job_id || job["runtime_id"] != id || job["action"] != "install" {
            return Err(AnvilError::BackendFailed(
                "Installation job changed; check this runtime in Settings before retrying.".into(),
            ));
        }
        match job["status"].as_str() {
            Some("Succeeded") => return Ok(()),
            Some("Running") => {}
            Some("Failed") => {
                return Err(AnvilError::BackendFailed(
                    job["error"]
                        .as_str()
                        .unwrap_or("Installation failed")
                        .into(),
                ))
            }
            _ => {
                return Err(AnvilError::BackendFailed(
                    "Invalid installation status".into(),
                ))
            }
        }
        if Instant::now() >= deadline {
            return Err(AnvilError::Timeout(
                "Installation result is unknown. Check this runtime in Settings before retrying."
                    .into(),
            ));
        }
        std::thread::sleep(Duration::from_millis(500));
        job = controller::request(Request::AppJob { id: id.into() })
            .map_err(AnvilError::BackendFailed)?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn install_job_is_submitted_again_only_when_android_restarted_under_it() {
        // Job errors as the controller reports them for a managed device.
        let failed = |text: &str| Err(AnvilError::BackendFailed(text.into()));
        let mut jobs = vec![
            failed("APK installation failed: cmd: Failure calling service package: Broken pipe (32)"),
            failed("APK session failed: cmd: Can't find service: package"),
            failed("Android is restarting and its package service is not ready yet. Nothing was installed."),
            Ok(()),
        ].into_iter();
        let mut pauses = 0;
        apps::retry_after_restart(|| jobs.next().unwrap(), || { pauses += 1; true }).unwrap();
        assert_eq!((pauses, jobs.next().is_none()), (3, true));
        // Unknown outcomes and real rejections are reported after one attempt.
        for text in [
            "Android did not confirm installation within 5 minutes. It may still be optimizing the app or waiting for verification. Check the Android window and installed app list before retrying; the installation outcome is unknown.",
            "Android did not confirm installation in time. It may still be installing the app. Check the Android window and the installed app list before retrying; the installation outcome is unknown.",
            "Worker changed during installation; the installation outcome is unknown. Check before retrying.",
            "Invalid installation result; the installation outcome is unknown. Check before retrying.",
            "APK installation failed: Failure [INSTALL_FAILED_VERSION_DOWNGRADE]",
            "Installation job changed; check this runtime in Settings before retrying.",
        ] {
            let mut attempts = 0;
            let error = apps::retry_after_restart(|| { attempts += 1; failed(text) }, || panic!("must not wait: {text}")).unwrap_err();
            assert_eq!((attempts, error.to_string().contains(text)), (1, true));
        }
        let error = apps::retry_after_restart::<()>(
            || Err(AnvilError::Timeout("Installation result is unknown. Check this runtime in Settings before retrying.".into())),
            || panic!("must not wait"),
        ).unwrap_err();
        assert!(matches!(error, AnvilError::Timeout(_)));
    }
    #[test]
    fn frozen_shortcut_routes_to_existing_worker_without_starting_another() {
        let id = "r-11111111111111111111111111111111";
        let mut replies = vec![json!({"mode":"desktop"}), json!({"state":"Frozen"}),
            json!({"id":"job","runtime_id":id,"package":"com.example.app","action":"launch","status":"Succeeded"})].into_iter();
        launch_with(id, "com.example.app", |request| {
            assert!(!matches!(request, Request::Start { .. }));
            Ok(replies.next().unwrap())
        }, || {}).unwrap();
        assert!(replies.next().is_none());
    }
    #[test]
    fn stopped_runtime_boots_and_every_request_stays_scoped() {
        let id = "r-11111111111111111111111111111111";
        let mut replies = vec![json!({"mode":"desktop"}), json!({"state":"Stopped"}), json!({}), json!({"state":"Starting"}), json!({"state":"Running"}), json!({"id":"job", "runtime_id":id,"package":"com.example.app","action":"launch","status":"Running"}), json!({"id":"job", "runtime_id":id,"package":"com.example.app","action":"launch","status":"Succeeded"})].into_iter();
        launch_with(
            id,
            "com.example.app",
            |r| {
                assert_eq!(serde_json::to_value(r).unwrap()["id"], id);
                Ok(replies.next().unwrap())
            },
            || {},
        )
        .unwrap();
        assert!(replies.next().is_none());
    }
    #[test]
    fn runtime_shortcut_requests_full_ui_and_checks_its_job() {
        let id = "r-11111111111111111111111111111111";
        let mut replies = vec![json!({"mode":"desktop"}), json!({"state":"Running"}),
            json!({"id":"job","runtime_id":id,"package":"org.anvildroid.desktop","action":"full_ui","status":"Succeeded"})].into_iter();
        launch_with(id, "org.anvildroid.desktop", |request| {
            if let Request::AppAction {action, package, ..} = &request {
                assert_eq!(action, "full_ui");assert_eq!(package, "org.anvildroid.desktop");
            }
            Ok(replies.next().unwrap())
        }, || {}).unwrap();
        assert!(replies.next().is_none());
    }
    #[test]
    fn rejects_headless_and_replaced_jobs() {
        assert!(launch_with(
            "r-one",
            "com.example.app",
            |_| Ok(json!({"mode":"headless"})),
            || {}
        )
        .unwrap_err()
        .contains("Desktop"));
        let mut replies = vec![json!({"mode":"desktop"}),json!({"state":"Running"}),json!({"id":"job","runtime_id":"other","package":"com.example.app","action":"launch","status":"Succeeded"})].into_iter();
        assert!(launch_with(
            "r-one",
            "com.example.app",
            |_| Ok(replies.next().unwrap()),
            || {}
        )
        .unwrap_err()
        .contains("job changed"));
    }
}
