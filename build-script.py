#!/usr/bin/env python3

import argparse
import os
import platform
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional


# -----------------------------------------------------------------------------
# Constants

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
WORKSPACE_DIR = os.path.dirname(PACKAGE_DIR)
SOURCES_DIR = os.path.join(PACKAGE_DIR, "Sources")
SWIFTSYNTAX_DIR = os.path.join(SOURCES_DIR, "SwiftSyntax")
SWIFTSYNTAXBUILDER_DIR = os.path.join(SOURCES_DIR, "SwiftSyntaxBuilder")
SWIFTSYNTAXPARSER_DIR = os.path.join(SOURCES_DIR, "SwiftSyntaxParser")
SWIFTSYNTAXBUILDERGENERATION_DIR = \
        os.path.join(SOURCES_DIR, "SwiftSyntaxBuilderGeneration")

LLVM_DIR = os.path.join(WORKSPACE_DIR, "llvm-project", "llvm")
SWIFT_DIR = os.path.join(WORKSPACE_DIR, "swift")

INCR_TRANSFER_ROUNDTRIP_EXEC = os.path.join(
    SWIFT_DIR, "utils", "incrparse", "incr_transfer_round_trip.py"
)

GYB_EXEC = os.path.join(SWIFT_DIR, "utils", "gyb")

LIT_EXEC = os.path.join(LLVM_DIR, "utils", "lit", "lit.py")

GROUP_INFO_PATH = os.path.join(PACKAGE_DIR, "utils", "group.json")

BASE_KIND_FILES = {
    "Decl": "SyntaxDeclNodes.swift",
    "Expr": "SyntaxExprNodes.swift",
    "Pattern": "SyntaxPatternNodes.swift",
    "Stmt": "SyntaxStmtNodes.swift",
    "Syntax": "SyntaxNodes.swift",
    "Type": "SyntaxTypeNodes.swift",
}


def fail_for_called_process_error(
    succinct_description: str,
    error: subprocess.CalledProcessError
) -> None:
    printerr(f"FAIL: {succinct_description}")
    printerr(f"Executing: {escapeCmd(error.cmd)}")
    printerr(error.output)
    raise SystemExit(1)


# -----------------------------------------------------------------------------
# Xcode Projects Generation


def xcode_gen(config: str) -> None:
    print("** Generate SwiftSyntax as an Xcode project **")
    os.chdir(PACKAGE_DIR)
    swiftpm_call = ["swift", "package", "generate-xcodeproj"]
    if config:
        swiftpm_call.extend(["--xcconfig-overrides", config])
    check_call(swiftpm_call)


# -----------------------------------------------------------------------------
# Helpers


def printerr(message: str) -> None:
    print(message, file=sys.stderr)


def note(message: str) -> None:
    print("--- %s: note: %s" % (os.path.basename(sys.argv[0]), message))
    sys.stdout.flush()


def fatal_error(message: str) -> None:
    printerr(message)
    raise SystemExit(1)


def escapeCmdArg(arg: str) -> str:
    if '"' in arg or " " in arg:
        return '"%s"' % arg.replace('"', '\\"')
    else:
        return arg


def escapeCmd(cmd: List[str]) -> str:
    return " ".join([escapeCmdArg(arg) for arg in cmd])


def call(cmd: List[str], env: Dict[str, str] = dict(os.environ),
         stdout: Optional[int] = None, stderr: Optional[int] = subprocess.STDOUT,
         verbose: bool = False) -> int:
    if verbose:
        print(escapeCmd(cmd))
    process = subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr)
    process.wait()

    return process.returncode


def check_call(cmd: List[str], cwd: Optional[str] = None,
               env: Dict[str, str] = dict(os.environ), verbose: bool = False) -> None:
    if verbose:
        print(escapeCmd(cmd))
    subprocess.check_call(cmd, cwd=cwd, env=env, stderr=subprocess.STDOUT)


def realpath(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    return os.path.realpath(path)


# -----------------------------------------------------------------------------
# Generating gyb Files


def check_gyb_exec(gyb_exec: str) -> None:
    if not os.path.exists(gyb_exec):
        fatal_error(
            """
Error: Could not find gyb.
Looking at '%s'.

Make sure you have the main swift repo checked out next to the swift-syntax
repository.
Refer to README.md for more information.
"""
            % gyb_exec
        )


def check_rsync() -> None:
    if call(["rsync", "--version"], stdout=subprocess.DEVNULL) != 0:
        fatal_error("Error: Could not find rsync.")


def generate_single_gyb_file(
    gyb_exec: str,
    gyb_file: str,
    output_file_name: str,
    destination: str,
    temp_files_dir: str,
    add_source_locations: bool,
    additional_gyb_flags: List[str],
    verbose: bool,
) -> None:
    # Source locations are added by default by gyb, and cleared by passing
    # `--line-directive=` (nothing following the `=`) to the generator. Our
    # flag is the reverse; we don't want them by default, only if requested.
    line_directive_flags = [] if add_source_locations else ["--line-directive="]

    # Generate the new file
    gyb_command = [
        sys.executable,
        gyb_exec,
        gyb_file,
        "-o",
        os.path.join(temp_files_dir, output_file_name),
    ]
    gyb_command += line_directive_flags
    gyb_command += additional_gyb_flags

    check_call(gyb_command, verbose=verbose)

    # Copy the file if different from the file already present in
    # gyb_generated
    rsync_command = [
        "rsync",
        "--checksum",
        os.path.join(temp_files_dir, output_file_name),
        os.path.join(destination, output_file_name),
    ]

    check_call(rsync_command, verbose=verbose)


# Generate the `.swift` files for all `.gyb` files in `sources_dir`. If
# `destination_dir` is not `None`, the resulting files will be written to
# `destination_dir`, otherwise they will be written to
# `sources_dir/gyb_generated`.
def generate_gyb_files_helper(
    sources_dir: str,
    destination_dir: Optional[str],
    gyb_exec: str,
    add_source_locations: bool,
    verbose: bool,
) -> None:
    temp_files_dir = tempfile.gettempdir()
    make_dir_if_needed(temp_files_dir)

    if destination_dir is None:
        destination_dir = os.path.join(sources_dir, "gyb_generated")
    make_dir_if_needed(destination_dir)

    # Clear any *.swift files that are relics from the previous run.
    clear_gyb_files_from_previous_run(
        sources_dir, destination_dir, verbose)

    def generate_gyb_file(gyb_file: str) -> None:
        gyb_file_path = os.path.join(sources_dir, gyb_file)

        # Slice off the '.gyb' to get the name for the output file
        output_file_name = gyb_file[:-4]

        generate_single_gyb_file(
            gyb_exec,
            gyb_file_path,
            output_file_name,
            destination_dir,
            temp_files_dir,
            add_source_locations,
            additional_gyb_flags=[],
            verbose=verbose,
        )

    # Generate the new .swift files in `temp_files_dir` and only copy them
    # to `destiantion_dir` if they are different than the
    # files already residing there. This way we don't touch the generated .swift
    # files if they haven't changed and don't trigger a rebuild.
    gyb_files = [file for file in os.listdir(sources_dir) if file.endswith(".gyb")]
    with ThreadPoolExecutor() as executor:
        executor.map(generate_gyb_file, gyb_files)


# Generate the syntax node `.swift` files from `SyntaxNodes.swift.gyb.template`.
# `destination_dir` is not `None`, the resulting files will be written to
# `destination_dir/syntax_nodes`, otherwise they will be written to
# `sources_dir/gyb_generated/syntax_nodes`.
def generate_syntax_node_template_gyb_files(
    destination_dir: Optional[str],
    gyb_exec: str,
    add_source_locations: bool,
    verbose: bool
) -> None:
    temp_files_dir = tempfile.gettempdir()
    make_dir_if_needed(temp_files_dir)

    if destination_dir is None:
        destination_dir = os.path.join(SWIFTSYNTAX_DIR, "gyb_generated")

    template_destination = os.path.join(destination_dir, "syntax_nodes")

    make_dir_if_needed(template_destination)
    for previous_gyb_gen_file in os.listdir(template_destination):
        if previous_gyb_gen_file.endswith(".swift"):
            if previous_gyb_gen_file not in BASE_KIND_FILES.values():
                check_call(
                    ["rm", previous_gyb_gen_file],
                    cwd=template_destination,
                    verbose=verbose,
                )

    for base_kind in BASE_KIND_FILES:
        output_file_name = BASE_KIND_FILES[base_kind]

        gyb_file = os.path.join(
            SWIFTSYNTAX_DIR, "SyntaxNodes.swift.gyb.template"
        )

        generate_single_gyb_file(
            gyb_exec,
            gyb_file,
            output_file_name,
            template_destination,
            temp_files_dir,
            add_source_locations,
            additional_gyb_flags=["-DEMIT_KIND=%s" % base_kind],
            verbose=verbose,
        )


def generate_gyb_files(
    gyb_exec: str, verbose: bool, add_source_locations: bool,
    swiftsyntax_destination: Optional[str] = None,
    swiftsyntaxbuilder_destination: Optional[str] = None,
    swiftsyntaxparser_destination: Optional[str] = None,
    swiftsyntaxbuildergenerator_destination: Optional[str] = None
) -> None:
    print("** Generating gyb Files **")

    check_gyb_exec(gyb_exec)
    check_rsync()

    generate_gyb_files_helper(
        SWIFTSYNTAX_DIR,
        swiftsyntax_destination,
        gyb_exec,
        add_source_locations,
        verbose
    )
    generate_gyb_files_helper(
        SWIFTSYNTAXBUILDER_DIR,
        swiftsyntaxbuilder_destination,
        gyb_exec,
        add_source_locations,
        verbose
    )
    generate_gyb_files_helper(
        SWIFTSYNTAXPARSER_DIR,
        swiftsyntaxparser_destination,
        gyb_exec,
        add_source_locations,
        verbose
    )
    generate_gyb_files_helper(
        SWIFTSYNTAXBUILDERGENERATION_DIR,
        swiftsyntaxbuildergenerator_destination,
        gyb_exec,
        add_source_locations,
        verbose
    )
    generate_syntax_node_template_gyb_files(
        swiftsyntax_destination,
        gyb_exec,
        add_source_locations,
        verbose
    )

    print("** Done Generating gyb Files **")


def run_code_generation(
    toolchain: str, build_dir: Optional[str], multiroot_data_file: Optional[str],
    release: bool, verbose: bool, swiftsyntaxbuilder_destination: str
) -> None:
    print("** Running code generation **")
    swiftpm_call = get_swiftpm_invocation(
        toolchain=toolchain,
        action="run",
        build_dir=build_dir,
        multiroot_data_file=multiroot_data_file,
        release=release,
    )

    swiftpm_call.extend([
        "SwiftSyntaxBuilderGeneration", swiftsyntaxbuilder_destination
    ])

    if verbose:
        swiftpm_call.extend(["--verbose"])

    env = dict(os.environ)
    env["SWIFT_BUILD_SCRIPT_ENVIRONMENT"] = "1"
    # Tell other projects in the unified build to use local dependencies
    env["SWIFTCI_USE_LOCAL_DEPS"] = "1"
    env["SWIFT_SYNTAX_PARSER_LIB_SEARCH_PATH"] = \
        os.path.join(toolchain, "lib", "swift", "macosx")
    check_call(swiftpm_call, env=env, verbose=verbose)


def make_dir_if_needed(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path)


# Remove any files in the `gyb_generated` directory that no longer have a
# corresponding `.gyb` file in the `Sources` directory.
def clear_gyb_files_from_previous_run(
    sources_dir: str, destination_dir: str, verbose: bool
) -> None:
    for previous_gyb_gen_file in os.listdir(destination_dir):
        if previous_gyb_gen_file.endswith(".swift"):
            gyb_file = os.path.join(
                sources_dir, previous_gyb_gen_file + ".gyb"
            )
            if not os.path.exists(gyb_file):
                check_call(
                    ["rm", previous_gyb_gen_file],
                    cwd=destination_dir,
                    verbose=verbose
                )


# -----------------------------------------------------------------------------
# Building SwiftSyntax


def get_swiftpm_invocation(
    toolchain: str, action: str, build_dir: Optional[str],
    multiroot_data_file: Optional[str], release: bool
) -> List[str]:
    swift_exec = os.path.join(toolchain, "bin", "swift")

    swiftpm_call = [swift_exec, action]
    swiftpm_call.extend(["--package-path", PACKAGE_DIR])
    if platform.system() != "Darwin":
        swiftpm_call.extend(["--enable-test-discovery"])
    if release:
        swiftpm_call.extend(["--configuration", "release"])
    if build_dir:
        swiftpm_call.extend(["--build-path", build_dir])
    if multiroot_data_file:
        swiftpm_call.extend(["--multiroot-data-file", multiroot_data_file])

    return swiftpm_call


class Builder(object):
    swiftpm_call: List[str]
    verbose: bool
    toolchain: str

    def __init__(
        self,
        toolchain: str,
        build_dir: Optional[str],
        multiroot_data_file: Optional[str],
        release: bool,
        verbose: bool,
        disable_sandbox: bool = False,
    ) -> None:
        self.swiftpm_call = get_swiftpm_invocation(
            toolchain=toolchain,
            action="build",
            build_dir=build_dir,
            multiroot_data_file=multiroot_data_file,
            release=release,
        )
        if disable_sandbox:
            self.swiftpm_call.append("--disable-sandbox")
        if verbose:
            self.swiftpm_call.extend(["--verbose"])
        self.verbose = verbose
        self.toolchain = toolchain

    def build(self, product_name: str) -> None:
        print("** Building " + product_name + " **")
        command = list(self.swiftpm_call)
        command.extend(["--product", product_name])

        env = dict(os.environ)
        env["SWIFT_BUILD_SCRIPT_ENVIRONMENT"] = "1"
        # Tell other projects in the unified build to use local dependencies
        env["SWIFTCI_USE_LOCAL_DEPS"] = "1"
        env["SWIFT_SYNTAX_PARSER_LIB_SEARCH_PATH"] = \
            os.path.join(self.toolchain, "lib", "swift", "macosx")
        check_call(command, env=env, verbose=self.verbose)


# -----------------------------------------------------------------------------
# Testing


def verify_generated_files(gyb_exec: str, verbose: bool) -> None:
    user_swiftsyntax_generated_dir = os.path.join(
        SWIFTSYNTAX_DIR, "gyb_generated"
    )
    user_swiftsyntaxbuilder_generated_dir = os.path.join(
        SWIFTSYNTAXBUILDER_DIR, "gyb_generated"
    )
    user_swiftsyntaxparser_generated_dir = os.path.join(
        SWIFTSYNTAXPARSER_DIR, "gyb_generated"
    )
    user_swiftsyntaxbuildergeneration_generated_dir = os.path.join(
        SWIFTSYNTAXBUILDERGENERATION_DIR, "gyb_generated"
    )

    self_swiftsyntax_generated_dir = tempfile.mkdtemp()
    self_swiftsyntaxbuilder_generated_dir = tempfile.mkdtemp()
    self_swiftsyntaxparser_generated_dir = tempfile.mkdtemp()
    self_swiftsyntaxbuildergeneration_generated_dir = tempfile.mkdtemp()

    generate_gyb_files(
        gyb_exec,
        verbose=verbose,
        add_source_locations=False,
        swiftsyntax_destination=self_swiftsyntax_generated_dir,
        swiftsyntaxbuilder_destination=self_swiftsyntaxbuilder_generated_dir,
        swiftsyntaxparser_destination=self_swiftsyntaxparser_generated_dir,
        swiftsyntaxbuildergenerator_destination=self_swiftsyntaxbuildergeneration_generated_dir  # noqa: E501
    )

    check_generated_files_match(
        self_swiftsyntax_generated_dir,
        user_swiftsyntax_generated_dir
    )
    check_generated_files_match(
        self_swiftsyntaxbuilder_generated_dir,
        user_swiftsyntaxbuilder_generated_dir
    )
    check_generated_files_match(
        self_swiftsyntaxparser_generated_dir,
        user_swiftsyntaxparser_generated_dir
    )
    check_generated_files_match(
        self_swiftsyntaxbuildergeneration_generated_dir,
        user_swiftsyntaxbuildergeneration_generated_dir
    )


def verify_code_generated_files(
    toolchain: str, build_dir: Optional[str],  multiroot_data_file: Optional[str],
    release: bool, verbose: bool
) -> None:
    user_swiftsyntaxbuilder_generated_dir = os.path.join(
        SWIFTSYNTAXBUILDER_DIR, "generated"
    )

    self_swiftsyntaxbuilder_generated_dir = tempfile.mkdtemp()

    try:
        run_code_generation(
            toolchain=toolchain,
            build_dir=realpath(build_dir),
            multiroot_data_file=multiroot_data_file,
            release=release,
            verbose=verbose,
            swiftsyntaxbuilder_destination=self_swiftsyntaxbuilder_generated_dir
        )
    except subprocess.CalledProcessError as e:
        fail_for_called_process_error(
            "Source generation using SwiftSyntaxBuilder failed",
            e
        )

    print("** Verifing code generated files **")

    check_generated_files_match(
        self_swiftsyntaxbuilder_generated_dir,
        user_swiftsyntaxbuilder_generated_dir
    )


def check_generated_files_match(self_generated_dir: str,
                                user_generated_dir: str) -> None:
    command = [
        "diff",
        "--recursive",
        "--exclude",
        ".*",  # Exclude dot files like .DS_Store
        "--context=0",
        self_generated_dir,
        user_generated_dir,
    ]
    check_call(command)


def verify_c_syntax_nodes_match() -> None:
    print("** Validating that the C data types match **")

    swift_syntax_c_definitions = os.path.join(
        SOURCES_DIR, "_CSwiftSyntax", "include", "c-syntax-nodes.h")
    swiftc_c_definitions = os.path.join(
        SWIFT_DIR, "include", "swift-c", "SyntaxParser",
        "SwiftSyntaxCDataTypes.h")

    check_call([
        "diff",
        swift_syntax_c_definitions,
        swiftc_c_definitions,
    ])


def run_tests(
    toolchain: str, build_dir: Optional[str], multiroot_data_file: Optional[str],
    release: bool, filecheck_exec: Optional[str], skip_lit_tests: bool, verbose: bool
) -> None:
    print("** Running SwiftSyntax Tests **")

    if not skip_lit_tests:
        run_lit_tests(
            toolchain=toolchain,
            build_dir=build_dir,
            release=release,
            filecheck_exec=filecheck_exec,
            verbose=verbose,
        )

    run_xctests(
        toolchain=toolchain,
        build_dir=build_dir,
        multiroot_data_file=multiroot_data_file,
        release=release,
        verbose=verbose,
    )

# -----------------------------------------------------------------------------
# Lit Tests


def check_lit_exec() -> None:
    if not os.path.exists(LIT_EXEC):
        fatal_error(
            """
Error: Could not find lit.py.
Looking at '%s'.

Make sure you have the llvm repo checked out next to the swift-syntax repo.
Refer to README.md for more information.
"""
            % LIT_EXEC
        )


def check_incr_transfer_roundtrip_exec() -> None:
    if not os.path.exists(INCR_TRANSFER_ROUNDTRIP_EXEC):
        fatal_error(
            """
Error: Could not find incr_transfer_round_trip.py.

Make sure you have the main swift repo checked out next to the swift-syntax
repo.
Refer to README.md for more information.
"""
        )


def find_lit_test_helper_exec(
    toolchain: str, build_dir: Optional[str], release: bool
) -> str:
    swiftpm_call = get_swiftpm_invocation(
        toolchain=toolchain,
        action="build",
        build_dir=build_dir,
        multiroot_data_file=None,
        release=release,
    )
    swiftpm_call.extend(["--product", "lit-test-helper"])
    swiftpm_call.extend(["--show-bin-path"])

    bin_dir = subprocess.check_output(swiftpm_call)
    return os.path.join(bin_dir.strip().decode('utf-8'), "lit-test-helper")


def run_lit_tests(toolchain: str, build_dir: Optional[str], release: bool,
                  filecheck_exec: Optional[str], verbose: bool) -> None:
    print("** Running lit-based tests **")

    check_lit_exec()
    check_incr_transfer_roundtrip_exec()

    lit_test_helper_exec = find_lit_test_helper_exec(
        toolchain=toolchain, build_dir=build_dir, release=release
    )

    lit_call = ["python3", LIT_EXEC]
    lit_call.append(os.path.join(PACKAGE_DIR, "lit_tests"))

    if filecheck_exec:
        lit_call.extend(["--param", "FILECHECK=" + filecheck_exec])
    if lit_test_helper_exec:
        lit_call.extend(["--param", "LIT_TEST_HELPER=" + lit_test_helper_exec])
    lit_call.extend(
        ["--param", "INCR_TRANSFER_ROUND_TRIP.PY=" + INCR_TRANSFER_ROUNDTRIP_EXEC]
    )

    # Print all failures
    lit_call.extend(["--verbose"])
    # Don't show all commands if verbose is not enabled
    if not verbose:
        lit_call.extend(["--succinct"])
    check_call(lit_call, verbose=verbose)


# -----------------------------------------------------------------------------
# XCTest Tests


def run_xctests(toolchain: str, build_dir: Optional[str],
                multiroot_data_file: Optional[str], release: bool,
                verbose: bool) -> None:
    print("** Running XCTests **")
    swiftpm_call = get_swiftpm_invocation(
        toolchain=toolchain,
        action="test",
        build_dir=build_dir,
        multiroot_data_file=multiroot_data_file,
        release=release,
    )

    if verbose:
        swiftpm_call.extend(["--verbose"])

    swiftpm_call.extend(["--test-product", "SwiftSyntaxPackageTests"])

    env = dict(os.environ)
    env["SWIFT_BUILD_SCRIPT_ENVIRONMENT"] = "1"
    # Tell other projects in the unified build to use local dependencies
    env["SWIFTCI_USE_LOCAL_DEPS"] = "1"
    env["SWIFT_SYNTAX_PARSER_LIB_SEARCH_PATH"] = \
        os.path.join(toolchain, "lib", "swift", "macosx")
    check_call(swiftpm_call, env=env, verbose=verbose)

# -----------------------------------------------------------------------------
# Arugment Parsing functions


def generate_xcodeproj(args: argparse.Namespace) -> None:
    xcode_gen(config=args.xcconfig_path)


def generate_source_code_command(args: argparse.Namespace) -> None:
    try:
        generate_gyb_files(
            args.gyb_exec,
            verbose=args.verbose,
            add_source_locations=args.add_source_locations,
        )
    except subprocess.CalledProcessError as e:
        fail_for_called_process_error("Generating .gyb files failed", e)

    try:
        if not args.gyb_only:
            destination = os.path.join(SWIFTSYNTAXBUILDER_DIR, "generated")
            run_code_generation(
                toolchain=args.toolchain,
                build_dir=realpath(args.build_dir),
                multiroot_data_file=args.multiroot_data_file,
                release=args.release,
                verbose=args.verbose,
                swiftsyntaxbuilder_destination=destination
            )
    except subprocess.CalledProcessError as e:
        fail_for_called_process_error(
            "Source generation using SwiftSyntaxBuilder failed", e)


def verify_source_code_command(args: argparse.Namespace) -> None:
    try:
        verify_generated_files(args.gyb_exec, verbose=args.verbose)

        verify_code_generated_files(
            toolchain=args.toolchain,
            build_dir=realpath(args.build_dir),
            multiroot_data_file=args.multiroot_data_file,
            release=args.release,
            verbose=args.verbose,
        )

        verify_c_syntax_nodes_match()
    except subprocess.CalledProcessError:
        printerr(
            "FAIL: Gyb-generated files committed to repository do " +
            "not match generated ones. Please re-generate the " +
            "gyb-files using the following command, open a PR to the " +
            "SwiftSyntax project and merge it alongside the main PR." +
            "$ swift-syntax/build-script.py generate-source-code " +
            "--toolchain /path/to/toolchain.xctoolchain/usr"
        )
        raise SystemExit(1)


def build_command(args: argparse.Namespace) -> None:
    try:
        builder = Builder(
            toolchain=args.toolchain,
            build_dir=realpath(args.build_dir),
            multiroot_data_file=args.multiroot_data_file,
            release=args.release,
            verbose=args.verbose,
            disable_sandbox=args.disable_sandbox,
        )
        # Until rdar://53881101 is implemented, we cannot request a build of multiple
        # targets simultaneously. For now, just build one product after the other.
        builder.build("SwiftSyntax")
        builder.build("SwiftSyntaxParser")
        builder.build("SwiftSyntaxBuilder")
        builder.build("SwiftSyntaxBuilderGeneration")
    except subprocess.CalledProcessError as e:
        fail_for_called_process_error("Building SwiftSyntax failed", e)


def test_command(args: argparse.Namespace) -> None:
    try:
        builder = Builder(
            toolchain=args.toolchain,
            build_dir=realpath(args.build_dir),
            multiroot_data_file=args.multiroot_data_file,
            release=args.release,
            verbose=args.verbose,
            disable_sandbox=args.disable_sandbox,
        )

        builder.build("lit-test-helper")

        run_tests(
            toolchain=args.toolchain,
            build_dir=realpath(args.build_dir),
            multiroot_data_file=args.multiroot_data_file,
            release=args.release,
            filecheck_exec=realpath(args.filecheck_exec),
            skip_lit_tests=args.skip_lit_tests,
            verbose=args.verbose,
        )
        print("** All tests passed **")
    except subprocess.CalledProcessError as e:
        fail_for_called_process_error("Running tests failed", e)


# -----------------------------------------------------------------------------
# Arugment Parsing

_DESCRIPTION = """
Build and test script for SwiftSyntax.

Build SwiftSyntax by generating all necessary files form the corresponding
.swift.gyb files first. For this, SwiftSyntax needs to be check out alongside
the main swift repo (http://github.com/apple/swift/) in the following structure

- (containing directory)
  - swift
  - swift-syntax

It is not necessary to build the compiler project.

The build script can also drive the test suite included in the SwiftSyntax
repo. This requires a custom build of the compiler project since it accesses
test utilities that are not shipped as part of the toolchains. See the Testing
section for arguments that need to be specified for this.
"""


def parse_args() -> argparse.Namespace:
    def add_default_build_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-r", "--release", action="store_true", help="Build in release mode."
        )

        parser.add_argument(
            "--build-dir",
            default=None,
            help="""
            The directory in which build products shall be put. If omitted
            a directory named ".build" will be put in the swift-syntax
            directory.
            """,
        )

        parser.add_argument(
            "--disable-sandbox",
            action="store_true",
            help="Disable sandboxes when building with SwiftPM",
        )

        parser.add_argument(
            "--multiroot-data-file",
            help="""
            Path to an Xcode workspace to create a unified build of SwiftSyntax with
            other projects.
            """,
        )

        parser.add_argument(
            "--toolchain",
            required=True,
            help="The path to the toolchain that shall be used to build SwiftSyntax.",
        )

        parser.add_argument(
            "-v", "--verbose", action="store_true", help="Enable verbose logging."
        )

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=_DESCRIPTION
    )

    # -------------------------------------------------------------------------
    # Shared arguments

    sub_parsers = parser.add_subparsers()

    # -------------------------------------------------------------------------
    generate_xcodeproj_parser = sub_parsers.add_parser(
        "generate-xcodeproj",
        help="Generate an Xcode project for SwiftSyntax."
    )
    generate_xcodeproj_parser.set_defaults(func=generate_xcodeproj)

    generate_xcodeproj_parser.add_argument(
        "--xcconfig-path",
        help="The path to an xcconfig file for generating Xcode projct.",
    )

    generate_xcodeproj_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging."
    )

    # -------------------------------------------------------------------------
    generate_source_code_parser = sub_parsers.add_parser("generate-source-code")
    generate_source_code_parser.set_defaults(func=generate_source_code_command)

    add_default_build_arguments(generate_source_code_parser)

    generate_source_code_parser.add_argument(
        "--gyb-only",
        action="store_true",
        help="""
        Only generate gyb templates (and not SwiftSyntaxBuilderGeneration's templates)
        """,
    )

    generate_source_code_parser.add_argument(
        "--gyb-exec",
        default=GYB_EXEC,
        help="Path to the gyb tool (default: %(default)s).",
    )

    generate_source_code_parser.add_argument(
        "--add-source-locations",
        action="store_true",
        help="""
        Insert ###sourceLocation comments in generated code for line-directive.
        """,
    )

    # -------------------------------------------------------------------------
    build_parser = sub_parsers.add_parser("build")
    build_parser.set_defaults(func=build_command)

    add_default_build_arguments(build_parser)

    # -------------------------------------------------------------------------
    test_parser = sub_parsers.add_parser("test")
    test_parser.set_defaults(func=test_command)

    add_default_build_arguments(test_parser)

    test_parser.add_argument(
        "--skip-lit-tests", action="store_true",
        help="Don't run lit-based tests"
    )

    test_parser.add_argument(
        "--filecheck-exec",
        default=None,
        help="""
        Path to the FileCheck executable that was built as part of the LLVM repository.
        If not specified, it will be looked up from PATH.
        """,
    )

    # -------------------------------------------------------------------------
    verify_source_code_parser = sub_parsers.add_parser("verify-source-code")
    verify_source_code_parser.set_defaults(func=verify_source_code_command)

    add_default_build_arguments(verify_source_code_parser)

    verify_source_code_parser.add_argument(
        "--gyb-exec",
        default=GYB_EXEC,
        help="Path to the gyb tool (default: %(default)s).",
    )

    return parser.parse_args()


# -----------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
