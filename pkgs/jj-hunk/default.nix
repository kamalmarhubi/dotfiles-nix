{
  lib,
  stdenv,
  rustPlatform,
  fetchFromGitHub,
  git,
  jujutsu,
}:
rustPlatform.buildRustPackage rec {
  pname = "jj-hunk";
  version = "0.4.1";

  src = fetchFromGitHub {
    owner = "laulauland";
    repo = "jj-hunk";
    rev = "v${version}";
    hash = "sha256-lFuYTg6TW/Lsz4wwaaWFi37F2aGKpLwQgq40VTdDUKE=";
  };

  cargoHash = "sha256-7yCA4a2NM20o7z757lbMtyvFC+72ScTd+N7AKWCH1KU=";

  nativeCheckInputs = [
    git
    jujutsu
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
    mkdir -p "$HOME/.config/jj"
    cat > "$HOME/.config/jj/config.toml" <<'TOML'
    [merge-tools.jj-hunk]
    program = "jj-hunk"
    edit-args = ["select", "$left", "$right"]
    TOML
    export PATH="$PWD/target/${stdenv.hostPlatform.rust.cargoShortTarget}/release:$PATH"
  '';

  meta = {
    description = "Non-interactive hunk distribution in jj CLI";
    homepage = "https://github.com/laulauland/jj-hunk";
    license = lib.licenses.mit;
    mainProgram = "jj-hunk";
  };
}
