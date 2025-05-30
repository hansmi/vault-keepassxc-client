#!/usr/bin/env python3

import argparse
import contextlib
import resource
import dataclasses
import enum
import getpass
import io
import json
import logging
import secrets
import subprocess
import sys
import tempfile

from ansible.config import manager as a_config_manager
from ansible.module_utils.common import yaml as a_yaml


_CONFIG_SECTION = "vault_keepassxc_client"
_CONFIG_DEFS = dict(
    VAULT_KEEPASSXC_CLIENT_HELPER=dict(
        type="string",
        description=[
            "Name of Git credential helper. May be an absolute path.",
        ],
        default="git-credential-keepassxc",
        ini=[dict(section=_CONFIG_SECTION, key="helper")],
        env=[dict(name="VAULT_KEEPASSXC_CLIENT_HELPER")],
    ),
    VAULT_KEEPASSXC_CLIENT_GROUP=dict(
        type="string",
        description=[
            "Name of KeepassXC group containing credentials. When using "
            "multiple databases it's recommended to use the same group name "
            "everywhere.",
        ],
        default="Ansible",
        ini=[dict(section=_CONFIG_SECTION, key="group")],
        env=[dict(name="VAULT_KEEPASSXC_CLIENT_GROUP")],
    ),
    VAULT_KEEPASSXC_CLIENT_DEFAULT_IDENTITY=dict(
        type="string",
        description=[
            "Vault identity to use when no ID is provided.",
        ],
        default="default",
        ini=[dict(section=_CONFIG_SECTION, key="default_identity")],
        env=[dict(name="VAULT_KEEPASSXC_CLIENT_DEFAULT_IDENTITY")],
    ),
)


@enum.unique
class Operation(enum.Enum):
    SET = enum.auto()
    GET = enum.auto()


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Config:
    helper: str
    group: str
    default_identity: str


def load_config():
    with tempfile.NamedTemporaryFile() as tmpfile:
        a_yaml.yaml_dump(_CONFIG_DEFS, stream=tmpfile, encoding="UTF-8")
        tmpfile.flush()

        mgr = a_config_manager.ConfigManager(defs_file=tmpfile.name)

    return Config(
        helper=mgr.get_config_value("VAULT_KEEPASSXC_CLIENT_HELPER"),
        group=mgr.get_config_value("VAULT_KEEPASSXC_CLIENT_GROUP"),
        default_identity=mgr.get_config_value(
            "VAULT_KEEPASSXC_CLIENT_DEFAULT_IDENTITY"
        ),
    )


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Params:
    verbose: bool
    helper: str
    group: str
    url: str
    vault_id: str
    generate_random: bool


def setup_logging(verbose):
    if verbose:
        fmt = "%(asctime)s %(levelname)-.1s %(message)s"
        level = logging.NOTSET
    else:
        fmt = "%(asctime)s %(message)s"
        level = logging.INFO

    logging.basicConfig(format=fmt, level=level)


@contextlib.contextmanager
def exit_on_exception(verbose):
    try:
        yield
    except Exception as exc:
        if verbose:
            logging.exception("Caught an exception")

        sys.exit(f"Error: {exc}")


def run_helper(params, *, args, req=None, capture_json=False):
    cmd = [params.helper]

    if params.verbose:
        cmd.append("-vvv")

    cmd.extend(args)

    buf = io.StringIO()

    if req:
        for key, value in sorted(req.items()):
            buf.write(key)
            buf.write("=")
            buf.write(value)
            buf.write("\n")

    logging.debug("Running helper: %r", cmd)

    if stdin := buf.getvalue().encode("UTF-8"):
        logging.debug("Standard input: %r", stdin)

    result = subprocess.run(
        cmd,
        check=True,
        input=stdin,
        stdout=(subprocess.PIPE if capture_json else None),
    )

    if capture_json:
        data = json.loads(result.stdout)
        logging.debug("Result: %r", data)
        return data

    return None


def do_get(params):
    logging.debug("Reading password for %r", params.url)

    response = run_helper(
        params,
        args=[
            "get",
            "--json",
            "--no-filter",
            "--group",
            params.group,
        ],
        req=dict(
            url=params.url,
        ),
        capture_json=True,
    )

    print(response["password"])


def do_set(params):
    if params.generate_random:
        password = secrets.token_urlsafe(nbytes=max(2 * secrets.DEFAULT_ENTROPY, 64))
    else:
        password = getpass.getpass(prompt="New password: ")

    logging.debug("Storing password for %r", params.url)

    run_helper(
        params,
        args=[
            "store",
            "--no-filter",
            "--create-in",
            params.group,
            "--group",
            params.group,
        ],
        req=dict(
            url=params.url,
            username=params.vault_id,
            password=password,
        ),
    )


def make_arg_parser():
    parser = argparse.ArgumentParser(
        description="Get Ansible vault password from a KeepassXC database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )

    parser.add_argument("--verbose", action="store_true", help="Enable verbose output.")

    parser.add_argument(
        "--vault-id",
        default=None,
        help="Vault identity. The ID must be a valid URL hostname.",
    )

    parser.add_argument(
        "--generate-random",
        action="store_true",
        help="Set a randomly generated password.",
    )

    parser.set_defaults(op=Operation.GET)

    group = parser.add_argument_group("Operation").add_mutually_exclusive_group()
    group.add_argument(
        "--get",
        dest="op",
        action="store_const",
        const=Operation.GET,
        help="Retrieve password from database (default).",
    )
    group.add_argument(
        "--set",
        dest="op",
        action="store_const",
        const=Operation.SET,
        help="Store password to database.",
    )

    return parser


def main_inner(args):
    logging.debug("Arguments: %r", args)

    cfg = load_config()

    logging.debug("Configuration: %r", cfg)

    vault_id = cfg.default_identity

    if args.vault_id:
        vault_id = args.vault_id

    p = Params(
        verbose=args.verbose,
        helper=cfg.helper,
        group=cfg.group,
        url=f"ansible-vault://{vault_id}/",
        vault_id=vault_id,
        generate_random=args.generate_random,
    )

    logging.debug("Parameters: %r", p)

    match args.op:
        case Operation.GET:
            do_get(p)
        case Operation.SET:
            do_set(p)
        case _:
            raise RuntimeError(f"Unknown operation {args.op!r}")


def main():
    # Disable coredumps
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    args = make_arg_parser().parse_args()

    setup_logging(args.verbose)

    with exit_on_exception(args.verbose):
        return main_inner(args)
