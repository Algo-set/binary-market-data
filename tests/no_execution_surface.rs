use std::fs;
use std::path::Path;

#[test]
fn crate_has_no_execution_or_secret_surface() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut text = fs::read_to_string(root.join("Cargo.toml")).unwrap();
    collect_source(&root.join("src"), &mut text);
    let lower = text.to_ascii_lowercase();
    for forbidden in [
        "create_order",
        "cancel_order",
        "post_order",
        "/orders",
        "private_key",
        "secret_key",
        "mnemonic",
        "eth_sendtransaction",
        "eth_sendrawtransaction",
        "polymarket-client-sdk",
        "alloy-signer",
        "ethers-signers",
    ] {
        assert!(
            !lower.contains(forbidden),
            "forbidden capability: {forbidden}"
        );
    }
    assert!(!text.contains("/Users/"));
}

fn collect_source(path: &Path, output: &mut String) {
    for entry in fs::read_dir(path).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            collect_source(&path, output);
        } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
            output.push_str(&fs::read_to_string(path).unwrap());
        }
    }
}
