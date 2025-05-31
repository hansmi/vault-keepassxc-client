from vault_keepassxc_client import cli


def test_load_config() -> None:
    assert cli.load_config() == cli.Config(
        helper="git-credential-keepassxc",
        group="Ansible",
        default_identity="default",
    )
