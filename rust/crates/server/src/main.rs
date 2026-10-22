//! Internal readiness service during migration. Learner/auth routes are not switched yet.
use axum::{
    Json, Router,
    extract::State,
    http::{StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
};
use clap::Parser;
use english_storage::{ReadError, ReadWorker};
use serde_json::json;
use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tokio::sync::Semaphore;

#[derive(Parser)]
struct Config {
    #[arg(long)]
    database: PathBuf,
    #[arg(long, default_value = "127.0.0.1:8001")]
    listen: SocketAddr,
    #[arg(long, default_value_t = false)]
    enable_api: bool,
    #[arg(long)]
    test_clock: Option<chrono::DateTime<chrono::Utc>>,
    #[arg(long)]
    static_dir: Option<PathBuf>,
}

#[derive(Clone)]
struct App {
    db: ReadWorker,
    slots: Arc<Semaphore>,
    api: Option<english_storage::api::Config>,
    database: PathBuf,
    builds: Arc<tokio::sync::Mutex<()>>,
    downloads: Arc<Semaphore>,
    reads: Vec<ReadWorker>,
    cursor: Arc<std::sync::atomic::AtomicUsize>,
    version_file: PathBuf,
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({"status":"ok"}))
}
async fn readiness(State(app): State<App>) -> Response {
    let Ok(_permit) = app.slots.try_acquire_owned() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "Service busy");
    };
    let result = app
        .db
        .call(Duration::from_secs(2), |conn| {
            conn.query_row(
                "SELECT revision FROM meaning_catalog_revision WHERE id=1",
                [],
                |r| r.get::<_, i64>(0),
            )
        })
        .await;
    match result {
        Ok(revision) => Json(json!({"status":"ok","schema_version":english_core::SCHEMA_VERSION,"content_revision":revision})).into_response(),
        Err(ReadError::Busy | ReadError::Deadline) => error(StatusCode::SERVICE_UNAVAILABLE, "Database busy"),
        Err(e) => { tracing::error!(error=%e,"readiness failed"); error(StatusCode::SERVICE_UNAVAILABLE, "Database unavailable") }
    }
}
fn error(status: StatusCode, detail: &str) -> Response {
    let mut response = (status, Json(json!({"detail":detail}))).into_response();
    if status == StatusCode::SERVICE_UNAVAILABLE || status == StatusCode::TOO_MANY_REQUESTS {
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, header::HeaderValue::from_static("1"));
    }
    response
}

#[tokio::main(worker_threads = 2)]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let config = Config::parse();
    anyhow::ensure!(
        config.listen.ip().is_loopback(),
        "migration server must bind to loopback"
    );
    let app = App {
        reads: if config.enable_api {
            vec![
                ReadWorker::open(&config.database, 16)?,
                ReadWorker::open(&config.database, 16)?,
            ]
        } else {
            Vec::new()
        },
        cursor: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        version_file: config
            .static_dir
            .or_else(|| std::env::var("STATIC_DIR").ok().map(PathBuf::from))
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../frontend/src")
            })
            .join("version.json"),
        db: if config.enable_api {
            ReadWorker::open_writer(&config.database, 16)?
        } else {
            ReadWorker::open(&config.database, 16)?
        },
        slots: Arc::new(Semaphore::new(16)),
        database: config.database.clone(),
        builds: Arc::new(tokio::sync::Mutex::new(())),
        downloads: Arc::new(Semaphore::new(4)),
        api: config.enable_api.then(|| english_storage::api::Config {
            cookie_secure: std::env::var("COOKIE_SECURE")
                .is_ok_and(|v| matches!(v.as_str(), "1" | "true" | "yes")),
            registration_code: std::env::var("REGISTRATION_CODE").unwrap_or_default(),
            default_timezone: std::env::var("DEFAULT_TIMEZONE")
                .unwrap_or_else(|_| "Asia/Shanghai".into()),
            clock: config.test_clock,
            rp_id: std::env::var("WEBAUTHN_RP_ID").unwrap_or_else(|_| "localhost".into()),
            rp_name: std::env::var("WEBAUTHN_RP_NAME")
                .unwrap_or_else(|_| "Context Vocabulary Trainer".into()),
            origins: std::env::var("WEBAUTHN_ORIGINS")
                .unwrap_or_else(|_| "http://localhost:5173,http://localhost:8000".into())
                .split(',')
                .map(|s| s.trim().to_owned())
                .filter(|s| !s.is_empty())
                .collect(),
        }),
    };
    let router = routes(app);
    let listener = tokio::net::TcpListener::bind(config.listen).await?;
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

fn routes(app: App) -> Router {
    Router::new()
        .route(
            "/api/health",
            get(health).layer(tower_http::limit::RequestBodyLimitLayer::new(65536)),
        )
        .route("/internal/ready", get(readiness))
        .route("/api/study/meaning-catalog", get(catalogue))
        .route("/api/version", get(version))
        .fallback(native_api)
        .layer(tower_http::limit::RequestBodyLimitLayer::new(131072))
        .layer(tower_http::set_header::SetResponseHeaderLayer::overriding(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("no-store"),
        ))
        .with_state(app)
}

async fn native_api(State(app): State<App>, request: axum::extract::Request) -> Response {
    let Some(config) = app.api else {
        return error(StatusCode::NOT_FOUND, "Route not migrated");
    };
    let Ok(_permit) = app.slots.try_acquire_owned() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "Service busy");
    };
    let method = request.method().to_string();
    let ordinary_read = method == "GET" && request.uri().path() != "/api/next";
    let worker = if ordinary_read && !app.reads.is_empty() {
        app.reads[app
            .cursor
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            % app.reads.len()]
        .clone()
    } else {
        app.db.clone()
    };
    let path = percent_encoding::percent_decode_str(request.uri().path())
        .decode_utf8_lossy()
        .into_owned();
    let query = form_urlencoded::parse(request.uri().query().unwrap_or("").as_bytes())
        .into_owned()
        .collect();
    let request_cookies = request
        .headers()
        .get(header::COOKIE)
        .and_then(|h| h.to_str().ok())
        .map(|cookies| {
            cookies
                .split(';')
                .filter_map(|c| c.trim().split_once('='))
                .map(|(key, value)| (key.to_owned(), value.to_owned()))
                .collect::<std::collections::BTreeMap<_, _>>()
        })
        .unwrap_or_default();
    let token = request_cookies.get("cvt_session").cloned();
    let origin = request
        .headers()
        .get(header::ORIGIN)
        .and_then(|h| h.to_str().ok())
        .map(str::to_owned);
    let body_limit = if path.starts_with("/api/auth/passkeys/") {
        131072
    } else {
        65536
    };
    let body = match axum::body::to_bytes(request.into_body(), body_limit).await {
        Ok(bytes) if bytes.is_empty() => json!({}),
        Ok(bytes) => match serde_json::from_slice(&bytes) {
            Ok(value) => value,
            Err(_) => return error(StatusCode::UNPROCESSABLE_ENTITY, "Invalid JSON"),
        },
        Err(_) => return error(StatusCode::PAYLOAD_TOO_LARGE, "Request body too large"),
    };
    let request = english_storage::api::Request {
        method,
        path,
        query,
        body,
        token,
        origin,
        cookies: request_cookies,
    };
    match worker
        .call(
            Duration::from_secs(if ordinary_read { 2 } else { 10 }),
            move |conn| Ok(english_storage::api::handle(conn, &config, &request)),
        )
        .await
    {
        Ok(Ok(reply)) => {
            let mut response = Json(reply.body).into_response();
            for cookie in reply.cookies {
                if let Ok(header) = header::HeaderValue::from_str(&cookie) {
                    response.headers_mut().append(header::SET_COOKIE, header);
                }
            }
            response
        }
        Ok(Err(e)) if e.status < 500 => {
            let status = StatusCode::from_u16(e.status).unwrap_or(StatusCode::BAD_REQUEST);
            if let Some(detail) = e.validation {
                (status, Json(json!({"detail":detail}))).into_response()
            } else {
                error(status, &e.detail)
            }
        }
        Ok(Err(e)) if e.status == 503 => error(StatusCode::SERVICE_UNAVAILABLE, &e.detail),
        Ok(Err(e)) => {
            tracing::error!(error=%e,"native API failed");
            error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error")
        }
        Err(e) => {
            tracing::warn!(error=%e,"native API unavailable");
            error(StatusCode::SERVICE_UNAVAILABLE, "Service busy")
        }
    }
}

async fn version(State(app): State<App>) -> Json<serde_json::Value> {
    let version = tokio::fs::read(&app.version_file)
        .await
        .ok()
        .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok())
        .and_then(|value| value["version"].as_str().map(str::to_owned));
    Json(json!({"version":version}))
}

async fn catalogue(State(app): State<App>, request: axum::extract::Request) -> Response {
    let Some(config) = app.api.clone() else {
        return error(StatusCode::NOT_FOUND, "Route not migrated");
    };
    let Ok(_admission) = app.slots.clone().try_acquire_owned() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "Service busy");
    };
    let token = request
        .headers()
        .get(header::COOKIE)
        .and_then(|h| h.to_str().ok())
        .and_then(|s| {
            s.split(';')
                .filter_map(|p| p.trim().split_once('='))
                .find(|(k, _)| *k == "cvt_session")
                .map(|(_, v)| v.to_owned())
        });
    let etag = request
        .headers()
        .get(header::IF_NONE_MATCH)
        .and_then(|h| h.to_str().ok())
        .map(str::to_owned);
    let auth = english_storage::api::Request {
        method: "GET".into(),
        path: "/api/study/meaning-catalog".into(),
        query: Default::default(),
        body: json!({}),
        token,
        origin: None,
        cookies: Default::default(),
    };
    let result = app
        .db
        .call(Duration::from_secs(2), move |conn| {
            use english_storage::api::{accounts, fault, n, one, s};
            let result = (|| {
                let user = accounts::authenticate(conn, &config, &auth)?;
                if s(&user, "role") != "learner" {
                    return Err(fault(
                        403,
                        "Learning is unavailable for administrator accounts",
                    ));
                }
                let revision = one(
                    conn,
                    "SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",
                    &[],
                )?
                .ok_or_else(|| fault(500, "Missing content revision"))?;
                Ok::<_, english_storage::api::Fault>((
                    n(&user, "id"),
                    s(&revision, "database_id").to_owned(),
                    n(&revision, "revision"),
                ))
            })();
            Ok(result)
        })
        .await;
    let (user, identity, revision) = match result {
        Ok(Ok(value)) => value,
        Ok(Err(e)) => {
            return error(
                StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
                &e.detail,
            );
        }
        Err(_) => return error(StatusCode::SERVICE_UNAVAILABLE, "Database busy"),
    };
    let Ok(build_lock) =
        tokio::time::timeout(Duration::from_secs(30), app.builds.clone().lock_owned()).await
    else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "Catalogue busy");
    };
    let database = app.database.clone();
    let file = tokio::task::spawn_blocking(move || -> anyhow::Result<(std::fs::File, String)> {
        let _build_lock = build_lock;
        use fs2::FileExt;
        use std::io::{BufWriter, Write};
        let directory = database.with_extension("catalog-cache");
        std::fs::create_dir_all(&directory)?;
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(directory.join("build.lock"))?;
        lock.try_lock_exclusive()?;
        let stem = format!("v1-{identity}-{user}-{revision}");
        let target = directory.join(format!("{stem}.json"));
        let metadata = directory.join(format!("{stem}.etag"));
        if target.is_file() && metadata.is_file() {
            return Ok((
                std::fs::File::open(target)?,
                std::fs::read_to_string(metadata)?,
            ));
        }
        let mut temp = tempfile::NamedTempFile::new_in(&directory)?;
        let result = english_content::write_catalog(
            &database,
            user,
            &identity,
            revision,
            BufWriter::with_capacity(65536, &mut temp),
            Duration::from_secs(30),
        )?;
        temp.as_file().sync_all()?;
        temp.persist(&target)?;
        let tag = format!("\"{}\"", result.sha256);
        let mut meta = tempfile::NamedTempFile::new_in(&directory)?;
        meta.write_all(tag.as_bytes())?;
        meta.as_file().sync_all()?;
        meta.persist(metadata)?;
        Ok((std::fs::File::open(target)?, tag))
    })
    .await;
    let (file, tag) = match file {
        Ok(Ok(value)) => value,
        other => {
            tracing::warn!(error=?other,"catalogue build failed");
            return error(StatusCode::SERVICE_UNAVAILABLE, "Catalogue unavailable");
        }
    };
    let Ok(tag_header) = header::HeaderValue::from_str(&tag) else {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "Invalid catalogue tag");
    };
    if etag.as_deref() == Some(tag.as_str()) {
        let mut response = StatusCode::NOT_MODIFIED.into_response();
        response.headers_mut().insert(header::ETAG, tag_header);
        return response;
    }
    let Ok(permit) = app.downloads.try_acquire_owned() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "Catalogue downloads busy");
    };
    let length = file.metadata().map(|m| m.len()).ok();
    let stream = async_stream::stream! {
        use tokio::io::AsyncReadExt;
        let _permit=permit;let mut file=tokio::fs::File::from_std(file);let end=tokio::time::Instant::now()+Duration::from_secs(180);
        loop {let mut buffer=vec![0u8;65536];let read=match tokio::time::timeout_at(end,file.read(&mut buffer)).await {
            Ok(Ok(value))=>value,Ok(Err(error))=>{yield Err(error);break;},Err(_)=>{yield Err(std::io::Error::new(std::io::ErrorKind::TimedOut,"catalogue transfer expired"));break;}
        };
            if read==0{break;}buffer.truncate(read);yield Ok::<_,std::io::Error>(axum::body::Bytes::from(buffer));}
    };
    let body = axum::body::Body::from_stream(stream);
    let mut response = body.into_response();
    response.headers_mut().insert(header::ETAG, tag_header);
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("application/json"),
    );
    if let Some(length) = length {
        response
            .headers_mut()
            .insert(header::CONTENT_LENGTH, header::HeaderValue::from(length));
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use tower::ServiceExt;

    #[tokio::test]
    async fn internal_routes_are_readonly_no_store_and_reject_unmigrated_api() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("test.db");
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA user_version=22; CREATE TABLE meaning_catalog_revision(id INTEGER, revision INTEGER); INSERT INTO meaning_catalog_revision VALUES(1,9);").unwrap();
        let router = routes(App {
            db: ReadWorker::open(&path, 2).unwrap(),
            slots: Arc::new(Semaphore::new(2)),
            api: None,
            database: path.clone(),
            builds: Arc::new(tokio::sync::Mutex::new(())),
            downloads: Arc::new(Semaphore::new(4)),
            reads: Vec::new(),
            cursor: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            version_file: path.with_file_name("version.json"),
        });
        for (path, status) in [
            ("/api/health", 200),
            ("/internal/ready", 200),
            ("/api/next", 404),
            ("/api/auth/me", 404),
        ] {
            let response = router
                .clone()
                .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), status);
            assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        }
        let response = router
            .oneshot(
                Request::builder()
                    .uri("/api/health")
                    .header(header::CONTENT_LENGTH, "65537")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(
            conn.query_row("SELECT revision FROM meaning_catalog_revision", [], |r| r
                .get::<_, i64>(
                0
            ))
            .unwrap(),
            9
        );
    }
}
