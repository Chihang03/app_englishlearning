//! Read-only migration foundation. No schema migration or content sync on open.
pub mod api;
use rusqlite::{Connection, OpenFlags};
use std::{
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use thiserror::Error;
use tokio::sync::{mpsc, oneshot};

pub fn open_readonly(path: &Path) -> rusqlite::Result<Connection> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    conn.busy_timeout(Duration::from_secs(1))?;
    conn.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        2 * 1024 * 1024,
    )?;
    conn.execute_batch("PRAGMA query_only=ON; PRAGMA foreign_keys=ON; PRAGMA cache_size=-2048; PRAGMA mmap_size=0;")?;
    let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if version != english_core::SCHEMA_VERSION {
        return Err(rusqlite::Error::InvalidQuery);
    }
    Ok(conn)
}

#[derive(Debug, Error)]
pub enum ReadError {
    #[error("database queue full")]
    Busy,
    #[error("database deadline exceeded")]
    Deadline,
    #[error("database worker unavailable")]
    Unavailable,
    #[error("database operation failed: {0}")]
    Sql(#[from] rusqlite::Error),
}

type Job = Box<dyn FnOnce(&mut Connection) + Send>;

#[derive(Clone)]
pub struct ReadWorker {
    sender: mpsc::Sender<Job>,
    slots: Arc<tokio::sync::Semaphore>,
}

struct Cancellation(Arc<AtomicBool>);
impl Drop for Cancellation {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}

impl ReadWorker {
    pub fn open(path: &Path, capacity: usize) -> Result<Self, ReadError> {
        Self::open_mode(path, capacity, false)
    }

    pub fn open_writer(path: &Path, capacity: usize) -> Result<Self, ReadError> {
        Self::open_mode(path, capacity, true)
    }

    fn open_mode(path: &Path, capacity: usize, write: bool) -> Result<Self, ReadError> {
        if capacity == 0 {
            return Err(ReadError::Busy);
        }
        let (sender, mut receiver) = mpsc::channel::<Job>(capacity);
        let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
        let path = path.to_owned();
        std::thread::Builder::new()
            .name("english-sqlite-read".into())
            .spawn(move || {
                let open = if write {
                    Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE).and_then(
                        |conn| {
                            conn.busy_timeout(Duration::from_secs(1))?;
                            conn.set_limit(
                                rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
                                2 * 1024 * 1024,
                            )?;
                            conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA cache_size=-2048;")?;
                            let version: i64 =
                                conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
                            if version != english_core::SCHEMA_VERSION {
                                return Err(rusqlite::Error::InvalidQuery);
                            }
                            Ok(conn)
                        },
                    )
                } else {
                    open_readonly(&path)
                };
                let mut conn = match open {
                    Ok(conn) => {
                        let _ = ready_tx.send(Ok(()));
                        conn
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        return;
                    }
                };
                while let Some(job) = receiver.blocking_recv() {
                    job(&mut conn);
                }
            })
            .map_err(|_| ReadError::Unavailable)?;
        ready_rx.recv().map_err(|_| ReadError::Unavailable)??;
        Ok(Self {
            sender,
            slots: Arc::new(tokio::sync::Semaphore::new(capacity + 1)),
        })
    }

    pub async fn call<T, F>(&self, budget: Duration, operation: F) -> Result<T, ReadError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> rusqlite::Result<T> + Send + 'static,
    {
        let deadline = Instant::now()
            .checked_add(budget)
            .ok_or(ReadError::Deadline)?;
        let slot = self
            .slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| ReadError::Busy)?;
        let cancelled = Arc::new(AtomicBool::new(false));
        let guard = Cancellation(cancelled.clone());
        let (tx, rx) = oneshot::channel();
        let job: Job = Box::new(move |conn| {
            let _slot = slot; // Remains occupied until actual work ends, even if the HTTP future is dropped.
            if cancelled.load(Ordering::Relaxed) || Instant::now() >= deadline {
                let _ = tx.send(Err(ReadError::Deadline));
                return;
            }
            let progress_flag = cancelled.clone();
            conn.progress_handler(
                1000,
                Some(move || progress_flag.load(Ordering::Relaxed) || Instant::now() >= deadline),
            );
            api::reset_budget();
            let result = operation(conn);
            conn.progress_handler(0, None::<fn() -> bool>);
            // A cancelled read transaction must never keep its WAL snapshot open.
            if !conn.is_autocommit() {
                let _ = conn.execute_batch("ROLLBACK");
            }
            let expired = cancelled.load(Ordering::Relaxed) || Instant::now() >= deadline;
            let _ = tx.send(if expired {
                Err(ReadError::Deadline)
            } else {
                result.map_err(ReadError::Sql)
            });
        });
        self.sender.try_send(job).map_err(|e| match e {
            mpsc::error::TrySendError::Full(_) => ReadError::Busy,
            mpsc::error::TrySendError::Closed(_) => ReadError::Unavailable,
        })?;
        let result = tokio::time::timeout(budget, rx)
            .await
            .map_err(|_| ReadError::Deadline)?
            .map_err(|_| ReadError::Unavailable)?;
        drop(guard);
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA user_version=22; CREATE TABLE marker(value INTEGER); INSERT INTO marker VALUES(7);").unwrap();
        (dir, path)
    }
    #[tokio::test]
    async fn readonly_rejects_writes_and_missing_database() {
        let (_dir, path) = fixture();
        let worker = ReadWorker::open(&path, 2).unwrap();
        assert!(
            worker
                .call(Duration::from_secs(1), |conn| conn
                    .execute("DELETE FROM marker", []))
                .await
                .is_err()
        );
        assert_eq!(
            worker
                .call(Duration::from_secs(1), |conn| conn.query_row(
                    "SELECT value FROM marker",
                    [],
                    |r| r.get::<_, i64>(0)
                ))
                .await
                .unwrap(),
            7
        );
        let missing = path.with_file_name("missing.db");
        assert!(ReadWorker::open(&missing, 2).is_err());
        assert!(!missing.exists());
    }
    #[tokio::test]
    async fn long_query_is_interrupted_and_connection_recovers() {
        let (_dir, path) = fixture();
        let worker = ReadWorker::open(&path, 2).unwrap();
        let start = Instant::now();
        let result = worker.call(Duration::from_millis(20), |conn| conn.query_row(
            "WITH RECURSIVE n(v) AS (VALUES(1) UNION ALL SELECT v+1 FROM n WHERE v<1000000000) SELECT sum(v) FROM n", [], |r| r.get::<_, i64>(0))).await;
        assert!(matches!(result, Err(ReadError::Deadline)));
        assert_eq!(
            worker
                .call(Duration::from_secs(1), |c| c.query_row(
                    "SELECT 42",
                    [],
                    |r| r.get::<_, i64>(0)
                ))
                .await
                .unwrap(),
            42
        );
        assert!(start.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn rejects_unknown_schema_without_migration() {
        let (_dir, path) = fixture();
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA user_version=23").unwrap();
        assert!(ReadWorker::open(&path, 2).is_err());
        assert_eq!(
            conn.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            23
        );
    }

    #[tokio::test]
    async fn queue_is_bounded_and_cancelled_queued_work_is_not_executed() {
        let (_dir, path) = fixture();
        let worker = ReadWorker::open(&path, 1).unwrap();
        let first = worker.clone();
        let (started_tx, started_rx) = oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
        let running = tokio::spawn(async move {
            first
                .call(Duration::from_secs(2), move |_| {
                    let _ = started_tx.send(());
                    release_rx.recv_timeout(Duration::from_secs(1)).unwrap();
                    Ok(1)
                })
                .await
        });
        started_rx.await.unwrap();
        let queued = worker.clone();
        let executed = Arc::new(AtomicBool::new(false));
        let flag = executed.clone();
        let waiting = tokio::spawn(async move {
            queued
                .call(Duration::from_secs(2), move |_| {
                    flag.store(true, Ordering::Relaxed);
                    Ok(2)
                })
                .await
        });
        for _ in 0..100 {
            if worker.sender.capacity() == 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert_eq!(worker.sender.capacity(), 0);
        assert!(matches!(
            worker.call(Duration::from_secs(1), |_| Ok(3)).await,
            Err(ReadError::Busy)
        ));
        waiting.abort();
        let _ = waiting.await;
        release_tx.send(()).unwrap();
        assert_eq!(running.await.unwrap().unwrap(), 1);
        for _ in 0..100 {
            if worker.slots.available_permits() == 2 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert!(!executed.load(Ordering::Relaxed));
        assert_eq!(worker.slots.available_permits(), 2);
    }
}
