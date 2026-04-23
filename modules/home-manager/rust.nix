{pkgs, ...}: {
  home.packages = with pkgs.fenix; [
    (combine ([
      (stable.withComponents [
        "cargo"
        "clippy"
        "rustc"
        "rust-src"
        "rustfmt"
      ])
    ] ++ (with targets; [
      x86_64-apple-darwin.stable.rust-std
      # Linux
      x86_64-unknown-linux-gnu.stable.rust-std
      x86_64-unknown-linux-musl.stable.rust-std
      aarch64-unknown-linux-gnu.stable.rust-std
      aarch64-unknown-linux-musl.stable.rust-std
      i686-unknown-linux-gnu.stable.rust-std
      i686-unknown-linux-musl.stable.rust-std
      arm-unknown-linux-gnueabi.stable.rust-std
      arm-unknown-linux-musleabi.stable.rust-std
      armv7-unknown-linux-gnueabihf.stable.rust-std
      powerpc64le-unknown-linux-gnu.stable.rust-std
      powerpc64-unknown-linux-gnu.stable.rust-std
      loongarch64-unknown-linux-gnu.stable.rust-std
      s390x-unknown-linux-gnu.stable.rust-std
      x86_64-unknown-linux-gnux32.stable.rust-std
      # BSD
      x86_64-unknown-freebsd.stable.rust-std
      i686-unknown-freebsd.stable.rust-std
      x86_64-unknown-netbsd.stable.rust-std
      # Wasm
      wasm32-unknown-unknown.stable.rust-std
    ])))
  ];
}
