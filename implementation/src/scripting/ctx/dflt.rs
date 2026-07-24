use std::{collections::BTreeMap, sync::Arc};

use crate::{
    common::{ErrorKind, LoggerWithId, MapUserError, ModuleError},
    scripting::{self, DEFAULT_LUA_SER_OPTIONS},
};
use anyhow::Context as _;
use base64::Engine;
use genvm_common::*;
use mlua::LuaSerdeExt;
use std::str::FromStr;

use super::req::Request;

use super::CtxPart;

static START: std::sync::LazyLock<std::time::Instant> =
    std::sync::LazyLock::new(std::time::Instant::now);

/// Process-relative monotonic milliseconds: comparable only within one process,
/// not a wall clock. Exists because the Lua sandbox loads no os library.
fn monotonic_ms() -> mlua::Integer {
    let elapsed = START.elapsed().as_millis();
    elapsed.min(mlua::Integer::MAX as u128) as mlua::Integer
}

impl CtxPart {
    async fn request(&self, vm: &mlua::Lua, req: Request) -> anyhow::Result<mlua::Value> {
        log_trace!(request:serde = req; "received request");

        let is_json = req.json;
        let error_on_status = req.error_on_status;
        let url = req.url.as_str().to_owned();

        let body_size_limit = req.response_body_max_size.unwrap_or(usize::MAX);
        let client = if req.unfiltered {
            &self.client_unfiltered
        } else {
            &self.client
        };
        let request = req.into_reqwest(client)?;

        if is_json {
            let res = scripting::send_request_get_lua_compatible_response_json(
                &self.metrics,
                &url,
                request,
                error_on_status,
                body_size_limit,
            )
            .await?;
            Ok(vm.to_value_with(&res, DEFAULT_LUA_SER_OPTIONS)?)
        } else {
            let res = scripting::send_request_get_lua_compatible_response_bytes(
                &self.metrics,
                &url,
                request,
                error_on_status,
                body_size_limit,
            )
            .await?;
            Ok(vm.to_value_with(&res, DEFAULT_LUA_SER_OPTIONS)?)
        }
    }
}

pub fn create_global(
    vm: &mlua::Lua,
    config: &crate::common::ModuleBaseConfig,
) -> anyhow::Result<mlua::Value> {
    let dflt = vm.create_table()?;

    dflt.set("data_dir", config.data_dir.as_str())?;

    dflt.set("log_json", vm.create_function(|vm: &mlua::Lua, data: mlua::Value| {
        let mut as_serde: BTreeMap<String, genvm_modules_interfaces::GenericValue> = vm.from_value(data)?;

        let level = as_serde.remove("level");
        let level = level.and_then(|x| x.as_str().map(|x| x.to_owned())).map(|x| logger::Level::from_str(&x).unwrap_or(logger::Level::Info)).unwrap_or(logger::Level::Info);

        let script_message = as_serde.remove("message").and_then(|x| x.as_str().map(|x| x.to_owned())).unwrap_or_else(|| "<none>".to_owned());

        log_with_level_into!(level, &LoggerWithId, log:serde = as_serde, genvm_id:id = crate::common::get_genvm_id().0; "script_log: {script_message}");
        Ok(())
    })?)?;

    dflt.set(
        "user_error",
        vm.create_function(|vm: &mlua::Lua, data: mlua::Value| {
            let as_serde: ModuleError = vm.from_value(data)?;

            Err::<(), mlua::Error>(mlua::Error::ExternalError(Arc::new(as_serde)))
        })?,
    )?;

    dflt.set(
        "sleep_seconds",
        vm.create_async_function(|vm: mlua::Lua, data: mlua::Value| async move {
            let as_seconds: f32 = vm.from_value(data)?;
            tokio::time::sleep(tokio::time::Duration::from_secs_f32(as_seconds)).await;

            Ok(())
        })?,
    )?;

    dflt.set(
        "base64_encode",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            let encoded = base64::prelude::BASE64_STANDARD.encode(data.as_bytes());

            Ok(vm.create_string(encoded))
        })?,
    )?;

    dflt.set(
        "json_parse",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            let data: serde_json::Value = serde_json::from_slice(&data.as_bytes())
                .map_user_error(ErrorKind::DESERIALIZING, true)
                .map_err(scripting::anyhow_to_lua_error)?;

            vm.to_value_with(&data, DEFAULT_LUA_SER_OPTIONS)
        })?,
    )?;

    dflt.set(
        "json_stringify",
        vm.create_function(|vm: &mlua::Lua, data: mlua::Value| {
            let data: serde_json::Value = vm.from_value(data)?;
            let data = serde_json::to_string(&data).map_err(mlua::Error::external)?;

            let res = vm.to_value_with(&data, DEFAULT_LUA_SER_OPTIONS)?;
            Ok(res)
        })?,
    )?;

    dflt.set(
        "base64_decode",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            let decoded = base64::prelude::BASE64_STANDARD
                .decode(data.as_bytes())
                .map_user_error(ErrorKind::DESERIALIZING, true)
                .map_err(scripting::anyhow_to_lua_error)?;

            Ok(vm.create_string(decoded))
        })?,
    )?;

    dflt.set(
        "split_url",
        vm.create_function(
            |vm: &mlua::Lua, url: mlua::String| -> mlua::Result<mlua::Value> {
                let url_str = url.to_str()?;
                let url = match reqwest::Url::parse(&url_str) {
                    Ok(url) => url,
                    Err(_) => return Ok(mlua::Nil),
                };

                let ret = vm.create_table_from([
                    (
                        "schema",
                        mlua::Value::String(vm.create_string(url.scheme())?),
                    ),
                    (
                        "port",
                        if let Some(port) = url.port() {
                            // integer, not float: `tostring(443.0)` is `"443.0"`,
                            // which breaks `host:port` resolution in lib-web.lua
                            mlua::Value::Integer(port as mlua::Integer)
                        } else {
                            mlua::Value::Nil
                        },
                    ),
                    (
                        "host",
                        mlua::Value::String(if let Some(host) = url.host_str() {
                            vm.create_string(host)?
                        } else {
                            vm.create_string(b"")?
                        }),
                    ),
                ])?;
                Ok(mlua::Value::Table(ret))
            },
        )?,
    )?;

    dflt.set(
        "url_encode",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            let encoded =
                url::form_urlencoded::byte_serialize(&data.as_bytes()).collect::<String>();
            vm.create_string(encoded)
        })?,
    )?;

    dflt.set(
        "filter_text",
        vm.create_function(|vm: &mlua::Lua, data: (mlua::String, mlua::Value)| {
            let vals: Vec<super::filters::TextFilter> = vm.from_value(data.1)?;
            let res = super::filters::apply_filters(&data.0.to_str()?, &vals);
            vm.create_string(res)
        })?,
    )?;

    dflt.set(
        "filter_image",
        vm.create_function(|vm: &mlua::Lua, data: (mlua::String, mlua::Value)| {
            let vals: Vec<super::filters::ImageFilter> = vm.from_value(data.1)?;
            let res = super::filters::apply_image_filters(&data.0.as_bytes(), &vals)
                .map_err(scripting::anyhow_to_lua_error)?;
            vm.create_string(res)
        })?,
    )?;

    dflt.set(
        "as_user_error",
        vm.create_function(|vm: &mlua::Lua, args: mlua::Value| {
            log_trace!(name = args.type_name(); "casting to user error (1)");

            let err = match args.as_error() {
                None => return Ok(mlua::Value::Nil),
                Some(err) => err,
            };

            log_trace!(error:? = err; "casting to user error (2)");

            if let Some(err) = super::try_unwrap_err(err) {
                log_trace!(error:? = err; "casting to user error (3)");
                return vm.to_value_with(&err, DEFAULT_LUA_SER_OPTIONS);
            }

            Ok(mlua::Value::Nil)
        })?,
    )?;

    dflt.set(
        "request",
        vm.create_async_function(
            |vm: mlua::Lua, args: (mlua::Table, mlua::Value)| async move {
                let (zelf, req) = args;

                let zelf: mlua::AnyUserData = zelf.get("__ctx_dflt")?;
                let zelf: mlua::UserDataRef<scripting::LuaDArc<CtxPart>> = zelf
                    .borrow()
                    .with_context(|| "unboxing userdata")
                    .map_err(scripting::anyhow_to_lua_error)?;

                let mut request: Request = vm
                    .from_value(req)
                    .with_context(|| "deserializing request")
                    .map_err(scripting::anyhow_to_lua_error)?;

                if request.sign {
                    request
                        .add_rfc9421_sign_headers(&zelf)
                        .await
                        .map_err(mlua::Error::external)?;
                }

                let response = zelf
                    .request(&vm, request)
                    .await
                    .map_err(scripting::anyhow_to_lua_error)?;

                let result = vm.to_value_with(&response, DEFAULT_LUA_SER_OPTIONS)?;

                Ok(result)
            },
        )?,
    )?;

    use rand::TryRngCore;

    // All hashes return raw digest bytes.
    dflt.set(
        "sha2_256",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            let digest = ring::digest::digest(&ring::digest::SHA256, &data.as_bytes());
            vm.create_string(digest.as_ref())
        })?,
    )?;

    dflt.set(
        "sha3_256",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            use sha3::Digest as _;
            let digest = sha3::Sha3_256::digest(data.as_bytes());
            vm.create_string(digest.as_slice())
        })?,
    )?;

    dflt.set(
        "keccak256",
        vm.create_function(|vm: &mlua::Lua, data: mlua::String| {
            use sha3::Digest as _;
            let digest = sha3::Keccak256::digest(data.as_bytes());
            vm.create_string(digest.as_slice())
        })?,
    )?;

    dflt.set(
        "monotonic_ms",
        vm.create_function(|_: &mlua::Lua, _: ()| Ok(monotonic_ms()))?,
    )?;

    dflt.set(
        "random_bytes",
        vm.create_function(|vm: &mlua::Lua, length: usize| {
            let mut rng = rand::rngs::OsRng;
            let mut bytes = vec![0u8; length];
            rng.try_fill_bytes(&mut bytes)
                .map_err(|x| scripting::anyhow_to_lua_error(x.into()))?;
            vm.create_string(bytes)
        })?,
    )?;

    dflt.set(
        "random_float",
        vm.create_function(|_: &mlua::Lua, _: ()| {
            let mut rng = rand::rngs::OsRng;
            let float_size = 64;
            let precision = 52 + 1;
            let scale = 1.0 / ((1u64 << precision) as f64);

            let value: u64 = rng
                .try_next_u64()
                .map_err(|x| scripting::anyhow_to_lua_error(x.into()))?;
            let value = value >> (float_size - precision) as u64;

            Ok(scale * value as f64)
        })?,
    )?;

    Ok(mlua::Value::Table(dflt))
}

#[cfg(test)]
mod tests {
    use std::{collections::BTreeMap, sync::Arc};

    use super::*;

    fn config() -> crate::common::ModuleBaseConfig {
        crate::common::ModuleBaseConfig {
            bind_address: None,
            lua_script_path: String::new(),
            vm_count: 1,
            lua_path: String::new(),
            signer_url: Arc::from(""),
            signer_headers: Arc::new(BTreeMap::new()),
            data_dir: String::new(),
        }
    }

    fn dflt_table(vm: &mlua::Lua) -> mlua::Table {
        match create_global(vm, &config()).unwrap() {
            mlua::Value::Table(table) => table,
            _ => panic!("expected table"),
        }
    }

    fn hash_hex(name: &str, input: &[u8]) -> String {
        let vm = mlua::Lua::new();
        let dflt = dflt_table(&vm);
        let hash: mlua::Function = dflt.get(name).unwrap();
        let input = vm.create_string(input).unwrap();
        let digest: mlua::String = hash.call(input).unwrap();
        hex::encode(digest.as_bytes())
    }

    #[test]
    fn sha2_256_known_vectors() {
        assert_eq!(
            hash_hex("sha2_256", b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hash_hex("sha2_256", b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha3_256_known_vectors() {
        assert_eq!(
            hash_hex("sha3_256", b""),
            "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"
        );
        assert_eq!(
            hash_hex("sha3_256", b"abc"),
            "3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532"
        );
    }

    #[test]
    fn keccak256_known_vectors() {
        // The Ethereum keccak256 of the empty input.
        assert_eq!(
            hash_hex("keccak256", b""),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
        );
        assert_eq!(
            hash_hex("keccak256", b"abc"),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
        );
    }

    #[test]
    fn monotonic_ms_is_non_decreasing() {
        let vm = mlua::Lua::new();
        let dflt = dflt_table(&vm);
        let monotonic_ms: mlua::Function = dflt.get("monotonic_ms").unwrap();
        let first: mlua::Integer = monotonic_ms.call(()).unwrap();
        let second: mlua::Integer = monotonic_ms.call(()).unwrap();

        assert!(first >= 0);
        assert!(second >= first);
    }
}
