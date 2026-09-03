use genvm_common::*;
use std::sync::Arc;

type VmFuture<T, C, E> = std::pin::Pin<
    Box<dyn std::future::Future<Output = anyhow::Result<super::UserVM<T, C, E>>> + Send>,
>;

/// Builds a fresh VM. Kept so the pool can grow on demand rather than block when
/// every pre-allocated VM is checked out.
type VmFactory<T, C, E> = dyn Fn() -> VmFuture<T, C, E> + Send + Sync;

struct Inner<T, C, E: 'static> {
    free: crossbeam::queue::ArrayQueue<Arc<super::UserVM<T, C, E>>>,
    factory: Box<VmFactory<T, C, E>>,
}

/// A pool of Lua VMs with a fixed steady-state size. A VM is checked out
/// exclusively via [`Pool::get`], so a single `lua_State` is never driven by two
/// executions concurrently. When the pool is empty a fresh VM is built on demand
/// instead of waiting; VMs returned while the pool is already full are torn down,
/// so the steady-state count stays at the configured size.
pub struct Pool<T, C, E: 'static> {
    inner: Arc<Inner<T, C, E>>,
}

impl<T, C, E: 'static> Clone for Pool<T, C, E> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

/// Exclusive checkout of one VM. Dereferences to the underlying [`super::UserVM`]
/// and returns it to the pool on drop.
pub struct PoolGuard<T, C, E: 'static> {
    vm: Option<Arc<super::UserVM<T, C, E>>>,
    inner: Arc<Inner<T, C, E>>,
}

impl<T, C, E: 'static> std::ops::Deref for PoolGuard<T, C, E> {
    type Target = super::UserVM<T, C, E>;

    fn deref(&self) -> &Self::Target {
        self.vm
            .as_ref()
            .expect("vm is present until the guard is dropped")
    }
}

impl<T, C, E: 'static> Drop for PoolGuard<T, C, E> {
    fn drop(&mut self) {
        if let Some(vm) = self.vm.take() {
            // Return it to the pool. `push` gives the VM back when the pool is
            // full -- that happens only for a VM built on demand beyond the
            // configured size -- and dropping it here tears it down.
            let _ = self.inner.free.push(vm);
        }
    }
}

impl<T, C, E: 'static> Pool<T, C, E> {
    /// Checks out a VM: a pre-allocated one if the pool has any, otherwise a
    /// freshly built one. Never waits on other checkouts.
    pub async fn get(&self) -> anyhow::Result<PoolGuard<T, C, E>> {
        let vm = match self.inner.free.pop() {
            Some(vm) => vm,
            None => {
                log_warn!("vm pool empty, building a vm on demand");
                Arc::new((self.inner.factory)().await?)
            }
        };
        Ok(PoolGuard {
            vm: Some(vm),
            inner: self.inner.clone(),
        })
    }
}

pub async fn new<T, C, E, F, Fac>(cnt: usize, factory: Fac) -> anyhow::Result<Pool<T, C, E>>
where
    E: 'static,
    Fac: Fn() -> F + Send + Sync + 'static,
    F: std::future::Future<Output = anyhow::Result<super::UserVM<T, C, E>>> + Send + 'static,
{
    if cnt == 0 {
        anyhow::bail!("vm pool size must be >= 1");
    }
    let factory: Box<VmFactory<T, C, E>> = Box::new(move || Box::pin(factory()));

    let free = crossbeam::queue::ArrayQueue::new(cnt);
    for _i in 0..cnt {
        let vm = factory().await?;
        free.push(Arc::new(vm))
            .ok()
            .expect("queue capacity matches the number of VMs");
    }

    Ok(Pool {
        inner: Arc::new(Inner { free, factory }),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use genvm_common::sync::DArc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    async fn trivial_vm() -> anyhow::Result<super::super::UserVM<(), (), ()>> {
        let cfg = crate::common::ModuleBaseConfig {
            bind_address: None,
            lua_script_path: String::new(),
            vm_count: 1,
            lua_path: String::new(),
            signer_url: std::sync::Arc::from(""),
            signer_headers: std::sync::Arc::new(std::collections::BTreeMap::new()),
            data_dir: String::new(),
        };
        crate::scripting::UserVM::create(
            &cfg,
            |_vm| async { Ok(()) },
            Box::new(|_vm, _table, _e: &DArc<()>| Ok(())),
        )
        .await
    }

    #[tokio::test]
    async fn grows_on_demand_and_sheds_overflow() {
        let created = Arc::new(AtomicUsize::new(0));
        let counter = created.clone();
        let pool = new(2, move || {
            let counter = counter.clone();
            async move {
                counter.fetch_add(1, Ordering::SeqCst);
                trivial_vm().await
            }
        })
        .await
        .unwrap();
        assert_eq!(created.load(Ordering::SeqCst), 2, "pre-allocates vm_count");

        let g1 = pool.get().await.unwrap();
        let g2 = pool.get().await.unwrap();
        // pool is now empty: this checkout builds a VM on demand instead of waiting
        let g3 = pool.get().await.unwrap();
        assert_eq!(
            created.load(Ordering::SeqCst),
            3,
            "grows past the pre-allocated size"
        );

        drop(g1);
        drop(g2);
        // the queue holds only 2, so returning a third tears it down
        drop(g3);

        // the two returned VMs are reused; no new ones are built
        let _g4 = pool.get().await.unwrap();
        let _g5 = pool.get().await.unwrap();
        assert_eq!(created.load(Ordering::SeqCst), 3, "reuses returned vms");
    }
}
