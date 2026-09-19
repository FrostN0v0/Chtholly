"""The portable launcher: validate, explicitly prepare, or run real Entari."""

from __future__ import annotations

import sys
from pathlib import Path
import argparse
from collections.abc import Sequence

from .validation import python_issues, initialize_directories, validate_configuration
from .configuration import StartupError, load_configuration, prepare_runtime_config


def _failure(issues: Sequence[str]) -> int:
    sys.stderr.write("Startup prerequisites are not satisfied:" + "\n")
    for issue in issues:
        sys.stderr.write(f"- {issue}" + "\n")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate, prepare resources, or start Chtholly.")
    parser.add_argument("--config", type=Path, help="Use this Entari configuration file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check prerequisites without writing or downloading")
    mode.add_argument("--prepare", action="store_true", help="Prepare enabled resources without starting adapters")
    parser.add_argument(
        "--with-browser-deps", action="store_true", help="Also install browser OS dependencies during --prepare"
    )
    args = parser.parse_args(argv)
    if args.with_browser_deps and not args.prepare:
        parser.error("--with-browser-deps requires --prepare")
    if issues := python_issues():
        return _failure(issues)
    # Checks must not create bytecode files when importing the native parser.
    sys.dont_write_bytecode = True
    try:
        config = load_configuration(args.config)
        result = validate_configuration(config)
        issues = list(result.issues)
        from .resources import resource_issues, prepare_resources, configure_resources

        try:
            configure_resources(config.plugins, config.root)
        except (ValueError, RuntimeError) as error:
            issues.append(str(error))
        if not args.prepare:
            issues.extend(resource_issues(config.plugins, config.root))
        if issues:
            return _failure(issues)
        for note in result.notes:
            sys.stdout.write(note + "\n")
        if args.check:
            sys.stdout.write("Startup prerequisites checked; no files changed or resources downloaded." + "\n")
            return 0
        if args.prepare:
            try:
                prepare_resources(config.plugins, config.root, with_browser_deps=args.with_browser_deps)
            except (ValueError, RuntimeError) as error:
                return _failure([str(error)])
            initialize_directories(result)
            sys.stdout.write("Startup resources prepared; no adapters started." + "\n")
            return 0
        initialize_directories(result)
        prepare_runtime_config(config)
        from arclet.entari import Entari

        Entari.from_config(config.native).run()
        return 0
    except StartupError as error:
        return _failure([str(error)])
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Framework/driver errors can include credentials and interpolated YAML.
        return _failure(
            [
                "Startup failed; check configuration, storage permissions and locked dependencies. "
                "Exception values are withheld to protect credentials."
            ]
        )
