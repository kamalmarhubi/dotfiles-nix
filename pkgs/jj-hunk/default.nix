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
  version = "0.5.1";

  src = fetchFromGitHub {
    owner = "laulauland";
    repo = "jj-hunk";
    rev = "v${version}";
    hash = "sha256-Pe0rLEUMXmq+8eUMmjuu5KvFJ/aN53bTQ6/1rE2YcT0=";
  };

  cargoHash = "sha256-tO4oGY92AieYb1SY3ylWSkOlcIKadbZLKOY6nTzXo48=";

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
