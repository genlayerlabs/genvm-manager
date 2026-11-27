use std::env;
use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-env-changed=LSQLITE3_SRC");
    println!("cargo:rerun-if-env-changed=SQLITE_SRC");
    println!("cargo:rerun-if-env-changed=LUA_INCLUDE");
    println!("cargo:rerun-if-env-changed=LSQLITE3_PREBUILT");

    if env::var("LSQLITE3_PREBUILT").is_ok() {
        println!("cargo:rustc-link-lib=static=lsqlite3_bundle");
        return;
    }

    let lsqlite3_src = match env::var("LSQLITE3_SRC") {
        Ok(v) => PathBuf::from(v),
        Err(_) => {
            panic!("either LSQLITE3_PREBUILT or LSQLITE3_SRC must be set");
        }
    };
    // Replaces the older SQLite bundled with lsqlite3
    let sqlite_src = PathBuf::from(
        env::var("SQLITE_SRC").expect("SQLITE_SRC must be set when LSQLITE3_SRC is set"),
    );

    let lua_include =
        env::var("LUA_INCLUDE").expect("LUA_INCLUDE must be set when LSQLITE3_SRC is set");

    cc::Build::new()
        .file(sqlite_src.join("sqlite3.c"))
        .define("SQLITE_CORE", None)
        .include(&sqlite_src)
        .opt_level(2)
        .warnings(false)
        .compile("sqlite3");

    // Compiled from a copy: `#include "sqlite3.h"` would otherwise find the
    // bundled header next to it before any include path
    let lsqlite3_c = PathBuf::from(env::var("OUT_DIR").unwrap()).join("lsqlite3.c");
    std::fs::write(
        &lsqlite3_c,
        std::fs::read(lsqlite3_src.join("lsqlite3.c")).unwrap(),
    )
    .unwrap();
    cc::Build::new()
        .file(&lsqlite3_c)
        .include(&lua_include)
        .include(&sqlite_src)
        .opt_level(2)
        .warnings(false)
        .compile("lsqlite3");
}
