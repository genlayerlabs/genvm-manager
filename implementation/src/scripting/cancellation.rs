use std::{
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex, Weak},
};

trait Cancel: Send + Sync {
    fn cancel(&self);
}

// Lua 5.3 may retain suspended coroutines; the call scope must own their pending work.
struct Operation<F>(Mutex<Option<Pin<Box<F>>>>);

impl<F: Send> Cancel for Operation<F> {
    fn cancel(&self) {
        self.0.lock().unwrap().take();
    }
}

struct Scope {
    operations: Mutex<Option<Vec<Weak<dyn Cancel>>>>,
}

pub(super) struct Guard<'a>(&'a mlua::Lua, Arc<Scope>);

impl<'a> Guard<'a> {
    pub(super) fn enter(vm: &'a mlua::Lua) -> mlua::Result<Self> {
        if vm.app_data_ref::<Arc<Scope>>().is_some() {
            return Err(mlua::Error::external("concurrent Lua calls on one VM"));
        }
        let scope = Arc::new(Scope {
            operations: Mutex::new(Some(Vec::new())),
        });
        vm.set_app_data(scope.clone());
        Ok(Self(vm, scope))
    }
}

impl Drop for Guard<'_> {
    fn drop(&mut self) {
        self.0.remove_app_data::<Arc<Scope>>();
        let operations = self.1.operations.lock().unwrap().take().unwrap_or_default();
        for operation in operations.into_iter().filter_map(|op| op.upgrade()) {
            operation.cancel();
        }
    }
}

pub(crate) fn create_async_function<A, R, F, FR>(
    vm: &mlua::Lua,
    function: F,
) -> mlua::Result<mlua::Function>
where
    A: mlua::FromLuaMulti,
    R: mlua::IntoLuaMulti,
    F: Fn(mlua::Lua, A) -> FR + Send + 'static,
    FR: Future<Output = mlua::Result<R>> + Send + 'static,
{
    vm.create_async_function(move |vm, args| {
        let scope = vm.app_data_ref::<Arc<Scope>>().map(|scope| scope.clone());
        let future = function(vm, args);
        async move {
            let Some(scope) = scope else {
                return future.await;
            };
            let operation = Arc::new(Operation(Mutex::new(Some(Box::pin(future)))));
            {
                let mut operations = scope.operations.lock().unwrap();
                let Some(operations) = operations.as_mut() else {
                    return Err(mlua::Error::external("Lua call cancelled"));
                };
                operations.retain(|op| op.strong_count() != 0);
                let erased: Arc<dyn Cancel> = operation.clone();
                operations.push(Arc::downgrade(&erased));
            }
            std::future::poll_fn(|cx| {
                let mut future = operation.0.lock().unwrap();
                let Some(inner) = future.as_mut() else {
                    return std::task::Poll::Ready(Err(mlua::Error::external(
                        "Lua call cancelled",
                    )));
                };
                let result = inner.as_mut().poll(cx);
                if result.is_ready() {
                    future.take();
                }
                result
            })
            .await
        }
    })
}
