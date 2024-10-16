from _curses import window
import contextlib
import curses
import enum
import itertools
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import threading
import time
import typing

#
# CLI Frontend
#

UNICODE_SUPPORT = (
    sys.stdout.encoding.lower() == "utf-8"
    and sys.stdout.isatty()
    and os.getenv("TERM") not in ("dumb", "emacs", "linux")
)
SKIP_CHAR = "─" if UNICODE_SUPPORT else "-"
FAIL_CHAR = "✗" if UNICODE_SUPPORT else "E"
OK_CHAR = "✔" if UNICODE_SUPPORT else "."
RUNNING_CHAR = "▶" if UNICODE_SUPPORT else ">"

ERROR_TEXT: str | None = None


class StepFailure(Exception):
    pass


class StepSkipped(Exception):
    pass


class SimpleCLIFrontend:
    def __init__(self, *, show_output: bool, interactive_step_selection: bool):
        self.show_output = show_output
        self.interactive_step_selection = interactive_step_selection
        self.unicode = (
            sys.stdout.encoding.lower() == "utf-8"
            and sys.stdout.isatty()
            and os.getenv("TERM") not in ("dumb", "emacs", "linux")
        )

    @contextlib.contextmanager
    def run_step(self, name: str) -> typing.Generator[None, None, None]:
        try:
            print(name, end=" ", flush=True)
            yield

        except StepSkipped:
            print(SKIP_CHAR)

        except Exception as e:
            print(FAIL_CHAR)
            global ERROR_TEXT
            ERROR_TEXT = stringify_exception(e)
            raise SystemExit(1)

        else:
            print(OK_CHAR)

    def stop(self) -> None:
        pass

    def _ask_step(self, step: typing.Callable) -> bool:
        step_name = step.__doc__ or " ".join(step.__name__.split("_"))
        while True:
            try:
                answer = input(f"Run {step_name}? [Y/n] ")
                if answer.lower() in ("", "y", "yes"):
                    return True
                elif answer.lower() in ("n", "no"):
                    return False
            except KeyboardInterrupt:
                raise SystemExit(1)

    def select_steps(self, steps: typing.Sequence[typing.Callable]) -> typing.Sequence[typing.Callable]:
        if not self.interactive_step_selection:
            return steps

        return [step for step in steps if self._ask_step(step)]

    def run_commands(
        self,
        titel: str,
        *commands: list[str] | typing.Callable[[], None],
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None:
        _run_commands(self, titel, *commands, skip_condition=skip_condition, capture_output=not self.show_output)

    def run_script(
        self,
        title: str,
        script: str,
        *,
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None:
        _run_script(self, title, script, skip_condition=skip_condition, capture_output=not self.show_output)


#
# Curses frontend
#


def draw_line(stdscr: window, line, step_is_selected: bool, step_is_active: bool, step_name: str):
    stdscr.move(line, 0)

    if step_is_selected:
        stdscr.addstr(RUNNING_CHAR, curses.color_pair(3))
    else:
        stdscr.addstr(" ")

    if step_is_active:
        stdscr.addstr(f" {OK_CHAR} " if step_is_active else f" {FAIL_CHAR} ", curses.color_pair(1))
    else:
        stdscr.addstr(f" {FAIL_CHAR} ", curses.color_pair(2))

    stdscr.addstr(step_name)


def redraw_screen(
    stdscr: window,
    steps: typing.Sequence[typing.Callable],
    step_enabled_flags: list[bool],
    current_step: int,
    offset: int,
):
    max_y, _ = stdscr.getmaxyx()
    stdscr.clear()
    if max_y > 0:
        stdscr.addstr(0, 0, "Select:")

    offset_end = offset + max_y - 3
    steps_and_enabled = zip(steps[offset:offset_end], step_enabled_flags[offset:offset_end])

    for i, (step_function, step_enabled) in enumerate(steps_and_enabled):
        if max_y > i + 2:
            draw_line(
                stdscr,
                i + 2,
                i == current_step - offset,
                step_enabled,
                step_function.__doc__ or " ".join(step_function.__name__.split("_")),
            )

    stdscr.refresh()


def select_steps(stdscr: window, steps: typing.Sequence[typing.Callable]) -> typing.Sequence[typing.Callable]:
    curses.curs_set(0)  # Make the cursor invisible
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # Green for enabled steps
    curses.init_pair(2, curses.COLOR_RED, -1)  # Red for disabled steps
    curses.init_pair(3, curses.COLOR_YELLOW, -1)  # Yellow for selected step

    current_step = 0
    offset = 0
    enabled = [True for _ in steps]

    while True:
        redraw_screen(stdscr, steps, enabled, current_step, offset)

        c = stdscr.getch()
        max_y, _ = stdscr.getmaxyx()
        if c == ord(" "):
            enabled[current_step] = not enabled[current_step]

        elif c == curses.KEY_DOWN:
            current_step = min(current_step + 1, len(steps) - 1)

        elif c == curses.KEY_UP:
            current_step = max(current_step - 1, 0)

        elif c == ord("\n"):
            break

        elif c == ord("q"):
            exit(0)

        if current_step - offset >= max_y - 3:
            offset += 1
        if current_step < offset:
            offset -= 1

        if max_y - 3 > len(steps) - offset:
            offset = max(0, len(steps) - max_y - 3)

    return [step for step, e in zip(steps, enabled) if e]


class StepState(enum.Enum):
    SUCCESS = 1
    FAILURE = 2
    SKIPPED = 3
    RUNNING = 4


def draw_steps_progress(stdscr: window, lines: list[tuple[str, StepState]], spinner_char: str) -> None:
    stdscr.clear()
    max_y, _ = stdscr.getmaxyx()

    visible_lines = lines[-max_y:]

    for i, (name, state) in enumerate(visible_lines):
        if state == StepState.SUCCESS:
            stdscr.addstr(i, 0, f"{OK_CHAR} {name}")
        elif state == StepState.FAILURE:
            stdscr.addstr(i, 0, f"{FAIL_CHAR} {name}")
        elif state == StepState.SKIPPED:
            stdscr.addstr(i, 0, f"{SKIP_CHAR} {name}")
        elif state == StepState.RUNNING:
            stdscr.addstr(i, 0, f"{spinner_char} {name}")

    stdscr.refresh()


def stringify_lines(lines: list[tuple[str, StepState]]) -> str:
    icon = {
        StepState.SUCCESS: OK_CHAR,
        StepState.FAILURE: FAIL_CHAR,
        StepState.SKIPPED: SKIP_CHAR,
        StepState.RUNNING: RUNNING_CHAR,
    }
    return "\n".join([f"{icon[state]} {name}" for name, state in lines])


def stringify_exception(e: Exception) -> str:
    return e.stderr.decode("utf-8") if isinstance(e, subprocess.CalledProcessError) else str(e)


class CursesFrontend:
    def __init__(self, stdscr: window) -> None:
        self.delay = 0.05
        self.running = False
        self.lines: list[tuple[str, StepState]] = []
        self.spinner_chars = itertools.cycle(
            # "▁▂▃▄▅▆▇█▇▆▅▄▃▂"
            # "|/-\\"
            "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            # "⣾⣽⣻⢿⡿⣟⣯⣷"
            # ".oO@*"
            # "^>v<"
            # "-–—–"
            # "[{(<|>)}]"
        )
        self.stdscr = stdscr

    @contextlib.contextmanager
    def run_step(self, name: str) -> typing.Generator[None, None, None]:
        # start draw loop if not already running
        if not self.running:
            self.running = True
            threading.Thread(target=self._draw_loop, daemon=True).start()

        self.lines.append((name, StepState.RUNNING))

        try:
            yield
        except StepSkipped:
            self.lines[-1] = (name, StepState.SKIPPED)
        except Exception as e:
            self.lines[-1] = (name, StepState.FAILURE)

            global ERROR_TEXT
            ERROR_TEXT = stringify_lines(self.lines) + "\n" + stringify_exception(e)

            raise SystemExit(1)
        else:
            self.lines[-1] = (name, StepState.SUCCESS)

    def stop(self) -> None:
        self.running = False

    def _draw_loop(self):
        while self.running:
            draw_steps_progress(self.stdscr, self.lines, next(self.spinner_chars))
            time.sleep(self.delay)

    def select_steps(self, steps: typing.Sequence[typing.Callable]) -> typing.Sequence[typing.Callable]:
        return select_steps(self.stdscr, steps)

    def run_commands(
        self,
        titel: str,
        *commands: list[str] | typing.Callable[[], None],
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None:
        _run_commands(self, titel, *commands, skip_condition=skip_condition, capture_output=True)

    def run_script(
        self,
        title: str,
        script: str,
        *,
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None:
        _run_script(self, title, script, skip_condition=skip_condition, capture_output=True)


class UIFrontend(typing.Protocol):
    def run_step(self, name: str) -> typing.ContextManager[None]: ...

    def stop(self) -> None: ...

    def select_steps(
        self, steps: typing.Sequence[typing.Callable[["typing.Self"], None]]
    ) -> typing.Sequence[typing.Callable[["typing.Self"], None]]: ...

    def run_commands(
        self,
        titel: str,
        *commands: list[str] | typing.Callable[[], None],
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None: ...

    def run_script(
        self,
        title: str,
        script: str,
        *,
        skip_condition: typing.Callable[[], bool] | None = None,
    ) -> None: ...


@contextlib.contextmanager
def get_frontend() -> typing.Generator[UIFrontend, None, None]:
    import argparse

    parser = argparse.ArgumentParser(description="Setup VM")
    parser.add_argument("--fancy", action="store_true", help="Run with fancy curses")
    parser.add_argument("--unattended", action="store_true", help="Run without user interaction")
    parser.add_argument("--verbose", action="store_true", help="Show output of commands when not running in curses")
    args = parser.parse_args()

    if args.unattended or not sys.stdout.isatty() or not sys.stdin.isatty():
        yield SimpleCLIFrontend(interactive_step_selection=False, show_output=args.verbose)
        return

    if not args.fancy:
        yield SimpleCLIFrontend(interactive_step_selection=True, show_output=args.verbose)
        return

    try:
        stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        try:
            curses.start_color()
        except Exception:
            pass

        frontend = CursesFrontend(stdscr)
        yield frontend

    finally:
        # Set everything back to normal
        if "stdscr" in locals():
            stdscr.keypad(False)
            curses.echo()
            curses.nocbreak()
            curses.endwin()

        if "frontend" in locals():
            print(stringify_lines(frontend.lines))


#
# sudo loop
#


def sudo_loop() -> None:
    while True:
        try:
            subprocess.run(["sudo", "true"], check=True)
            time.sleep(10)
        except subprocess.CalledProcessError:
            pass


#
# Step Helper
#


def get_sudo() -> None:
    subprocess.run(["sudo", "true"], check=True)
    threading.Thread(target=sudo_loop, daemon=True).start()


def _run_commands(
    frontend: UIFrontend,
    titel: str,
    *commands: list[str] | typing.Callable[[], None],
    skip_condition: typing.Callable[[], bool] | None = None,
    capture_output: bool = True,
) -> None:
    with frontend.run_step(titel):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        for command in commands:
            if isinstance(command, list):
                subprocess.run(command, capture_output=capture_output, check=True)
            else:
                command()


def _run_script(
    frontend: UIFrontend,
    title: str,
    script: str,
    *,
    skip_condition: typing.Callable[[], bool] | None = None,
    capture_output: bool = True,
) -> None:
    with frontend.run_step(title):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        subprocess.run(
            script,
            shell=True,
            check=True,
            capture_output=capture_output,
        )


def are_packages_installed_check(name, *names: str):
    def skip_condition() -> bool:
        completed_process = subprocess.run(
            ["dpkg", "-s", name, *names],
            capture_output=True,
        )

        return completed_process.returncode == 0

    return skip_condition


def is_kernel_module_loaded(name):
    def skip_condition() -> bool:
        completed_process = subprocess.run(
            f"lsmod | grep '{name}'",
            capture_output=True,
            shell=True,
        )

        return completed_process.returncode == 0

    return skip_condition


def set_environment() -> None:
    os.environ["NEEDRESTART_MODE"] = "a"
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"


def is_command_available(command: str):
    def skip_condition() -> bool:
        return shutil.which(command) is not None

    return skip_condition


#
# Data
#

better_branch_script = """
#!/bin/bash

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NO_COLOR='\033[0m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
NO_COLOR='\033[0m'

width1=5
width2=6
width3=45
width4=20
width5=40

# Function to count commits
count_commits() {
    local branch="$1"
    local base_branch="$2"
    local ahead_behind

    ahead_behind=$(git rev-list --left-right --count "$base_branch"..."$branch")
    echo "$ahead_behind"
}

# Main script
main_branch=$(git rev-parse HEAD)

printf "${GREEN}%-${width1}s ${RED}%-${width2}s ${BLUE}%-${width3}s ${YELLOW}%-${width4}s ${NO_COLOR}%-${width5}s\n" "Ahead" "Behind" "Branch" "Last Commit"  " "

# Separator line for clarity
printf "${GREEN}%-${width1}s ${RED}%-${width2}s ${BLUE}%-${width3}s ${YELLOW}%-${width4}s ${NO_COLOR}%-${width5}s\n" "-----" "------" "------------------------------" "-------------------" " "


format_string="%(objectname:short)@%(refname:short)@%(committerdate:relative)"
IFS=$'\n'

for branchdata in $(git for-each-ref --sort=-authordate --format="$format_string" refs/heads/ --no-merged); do
    sha=$(echo "$branchdata" | cut -d '@' -f1)
    branch=$(echo "$branchdata" | cut -d '@' -f2)
    time=$(echo "$branchdata" | cut -d '@' -f3)
    if [ "$branch" != "$main_branch" ]; then
            # Get branch description
            description=$(git config branch."$branch".description)

            # Count commits ahead and behind
            ahead_behind=$(count_commits "$sha" "$main_branch")
            ahead=$(echo "$ahead_behind" | cut -f2)
            behind=$(echo "$ahead_behind" | cut -f1)

            # Display branch info
        printf "${GREEN}%-${width1}s ${RED}%-${width2}s ${BLUE}%-${width3}s ${YELLOW}%-${width4}s ${NO_COLOR}%-${width5}s\n" $ahead $behind $branch "$time" "$description"
    fi
done
"""  # noqa: E501


#
# Actual steps
#


def check_prerequisites(frontend: UIFrontend) -> None:
    with frontend.run_step("64-bit Linux"):
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise StepFailure()

    with frontend.run_step("don't run as root"):
        if os.getuid() == 0:
            raise StepFailure()

    with frontend.run_step("locate apt"):
        if shutil.which("apt") is None:
            raise StepFailure()

    with frontend.run_step("wget"):
        if shutil.which("wget") is None:
            try:
                subprocess.run(["sudo", "-n", "apt", "install", "-y", "-qq", "wget"], check=True, capture_output=True)
            except subprocess.CalledProcessError:
                raise StepFailure()

    with frontend.run_step("locate gpg"):
        if shutil.which("gpg") is None:
            raise StepFailure()

    (pathlib.Path.home() / ".config").mkdir(exist_ok=True, parents=True)


def update_system(frontend: UIFrontend):
    frontend.run_commands(
        "Update System",
        ["sudo", "-n", "apt", "update", "-qq"],
        ["sudo", "-n", "apt", "full-upgrade", "--auto-remove", "-y", "--purge", "-qq"],
    )


def setup_regolith_ubuntu_yammy(frontend: UIFrontend) -> None:
    def skip_condition() -> bool:
        releases_path = pathlib.Path("/etc/os-release")
        return (
            not releases_path.exists()
            or "UBUNTU_CODENAME=jammy" not in releases_path.read_text()
            or pathlib.Path("/etc/apt/sources.list.d/regolith.list").exists()
        )

    frontend.run_script(
        "Setup Regolith (yammy)",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo -n tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-ubuntu-jammy-amd64 jammy main" | sudo -n tee /etc/apt/sources.list.d/regolith.list
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt update -qq
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -y -qq regolith-system-ubuntu regolith-session-flashback regolith-look-lascaille regolith-lightdm-config lightdm
        mkdir -p ~/.config/regolith3
        echo "wm.gaps.focus_follows_mouse: true" >> ~/.config/regolith3/Xresources
        """,  # noqa: E501
        skip_condition=skip_condition,
    )


def setup_regolith_ubuntu_nobel(frontend: UIFrontend) -> None:
    def skip_condition() -> bool:
        releases_path = pathlib.Path("/etc/os-release")
        return (
            not releases_path.exists()
            or "UBUNTU_CODENAME=noble" not in releases_path.read_text()
            or pathlib.Path("/etc/apt/sources.list.d/regolith.list").exists()
        )

    frontend.run_script(
        "Setup Regolith (noble)",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo tee /usr/share/keyrings/regolith-archive-keyring.gpg > /dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-3_2-ubuntu-noble-amd64 noble main" | sudo tee /etc/apt/sources.list.d/regolith.list
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt update -qq
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -y -qq regolith-system-ubuntu regolith-session-flashback regolith-look-lascaille regolith-lightdm-config lightdm
        mkdir -p ~/.config/regolith3/
        echo "wm.gaps.focus_follows_mouse: true" >> ~/.config/regolith3/Xresources
        """,  # noqa: E501
        skip_condition=skip_condition,
    )


def setup_regolith_debian_bookworm(frontend: UIFrontend) -> None:
    def skip_condition() -> bool:
        releases_path = pathlib.Path("/etc/os-release")
        return (
            not releases_path.exists()
            or 'VERSION_ID="12"' not in releases_path.read_text()
            or pathlib.Path("/etc/apt/sources.list.d/regolith.list").exists()
        )

    frontend.run_script(
        "Setup Regolith (bookworm)",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo -n tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-3_1-debian-bookworm-amd64 bookworm main" | sudo -n tee /etc/apt/sources.list.d/regolith.list
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt update -qq
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -y -qq regolith-desktop regolith-session-flashback regolith-look-lascaille regolith-lightdm-config lightdm
        mkdir -p ~/
        echo "wm.gaps.focus_follows_mouse: true" >> ~/.config/regolith3/Xresources
        """,  # noqa: E501
        skip_condition=skip_condition,
    )


def setup_virtual_box_guest_additions(frontend: UIFrontend):
    def get_username():
        import pwd  # after unix check is done

        return pwd.getpwuid(os.getuid())[0]

    frontend.run_commands(
        "Virtualbox Dependencies",
        ["sudo", "-n", "apt", "install", "-y", "-qq", "dkms", "gcc", "perl", "bzip2"],
        skip_condition=are_packages_installed_check("dkms", "gcc", "perl"),
    )

    with frontend.run_step("Virtualbox Investigation"):
        # check if virtualbox guest additions are installed
        if shutil.which("VBoxClient") is not None:
            return

        # check if virtualbox guest additions are mounted under /media/USERNAME/VBox_GAs_*/
        vbox_gas_path = (
            guest_dirs[-1]
            if (
                guest_dirs := sorted(
                    (pathlib.Path("/media") / get_username()).glob("VBox_GAs_*/VBoxLinuxAdditions.run")
                )
            )
            else None
        )

        if not vbox_gas_path:
            # find all cd drives
            drives = pathlib.Path("/dev").glob("sr*")

            # try to mount virtualbox guest additions from all available cd drives
            mount_destination = pathlib.Path("/media") / get_username() / "VBox_GAs_Test"
            subprocess.run(["sudo", "-n", "mkdir", "-p", str(mount_destination)], capture_output=True, check=True)
            for drive in drives:
                mount_result = subprocess.run(
                    ["sudo", "-n", "mount", "-t", "iso9660", str(drive), str(mount_destination)],
                    capture_output=True,
                )
                if mount_result.returncode != 0:
                    continue

                # check whether the mounted cd drive contains the virtualbox guest additions
                run_path = mount_destination / "VBoxLinuxAdditions.run"
                if run_path.exists():
                    vbox_gas_path = run_path
                    break

                # unmount and try another drive
                subprocess.run(
                    ["sudo", "-n", "umount", str(mount_destination)],
                    check=True,
                    capture_output=True,
                )

        if not vbox_gas_path:
            raise StepFailure()

    frontend.run_script(
        "Virtualbox Guest Addtions",
        f"""
        cd {vbox_gas_path.parent}
        sudo -n ./VBoxLinuxAdditions.run --quiet
        sudo -n usermod -aG vboxsf $USER
        """,
        skip_condition=is_command_available("VBoxClient"),
    )


def zsh_ohmyzsh(frontend: UIFrontend):
    frontend.run_script(
        "zsh & ohmyzsh",
        """
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zsh git fzf
        sudo -n chsh -s "$(which zsh)" "$USER"
        sh -c "$(wget -qO- https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended

        git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions
        git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting
        zsh -c 'omz plugin enable fzf'
        zsh -c 'omz plugin enable zsh-autosuggestions'
        zsh -c 'omz plugin enable zsh-syntax-highlighting'

        mkdir -p "$HOME/.zsh"
        git clone https://github.com/sindresorhus/pure.git "$HOME/.zsh/pure"
        echo 'fpath+=($HOME/.zsh/pure)' >> $HOME/.zshrc
        echo 'autoload -U promptinit; promptinit' >> $HOME/.zshrc
        echo 'prompt pure' >> $HOME/.zshrc
        """,  # noqa: E501
        skip_condition=(pathlib.Path.home() / ".oh-my-zsh").is_dir,
    )


def update_alias(frontend: UIFrontend):
    with frontend.run_step("alias up"):
        config = pathlib.Path.home() / ".zshrc"
        config_text = config.read_text()

        if "alias up=" in config_text:
            raise StepSkipped()

        config.write_text(
            config_text
            + (
                "\nalias up='sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive "
                "apt update && sudo apt full-upgrade --auto-remove -y'\n"
            )
        )


def helper_tools(frontend: UIFrontend):
    tools = [
        "curl",
        "htop",
        "atool",
        "arc",
        "arj",
        "lzip",
        "lzop",
        "p7zip-full",
        "rar",
        "unace",
        "unalz",
        "unrar",
        "nano",  # editor
        "nnn",  # file manager
    ]

    frontend.run_commands(
        "Helper Tools",
        ["sudo", "-n", "apt", "install", "-y", "-qq", *tools],
        skip_condition=are_packages_installed_check(*tools),
    )


def vscode(frontend: UIFrontend) -> None:
    frontend.run_script(
        "vs code",
        """
        wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor >packages.microsoft.gpg
        sudo -n install -D -o root -g root -m 644 packages.microsoft.gpg /etc/apt/keyrings/packages.microsoft.gpg
        sudo -n sh -c 'echo "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" > /etc/apt/sources.list.d/vscode.list'
        rm -f packages.microsoft.gpg

        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -qq -y apt-transport-https
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt update -qq
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -qq -y code
        """,  # noqa: E501
        skip_condition=pathlib.Path("/etc/apt/sources.list.d/vscode.list").exists,
    )


def devops_ssh(frontend: UIFrontend):
    config_path = pathlib.Path.home() / ".ssh" / "config"

    def skip_condition() -> bool:
        if not config_path.exists():
            return False

        return "Host ssh.dev.azure.com" in config_path.read_text()

    def append_ssh_config() -> None:
        config_path.touch(0o600)
        config_path.write_text(
            "\n".join(
                [
                    config_path.read_text(),
                    "Host ssh.dev.azure.com",
                    "    User git",
                    "    PubkeyAcceptedAlgorithms +ssh-rsa",
                    "    HostkeyAlgorithms +ssh-rsa",
                    "",
                ]
            )
        )

    frontend.run_commands(
        "Azure Devops",
        ["sudo", "-n", "apt", "install", "-y", "-qq", "ssh"],
        ["mkdir", "-p", str(config_path.parent)],
        ["chmod", "700", str(config_path.parent)],
        append_ssh_config,
        skip_condition=skip_condition,
    )


def watchdog(frontend: UIFrontend):
    frontend.run_commands(
        "install watchdog",
        ["sudo", "-n", "apt", "install", "-qq", "-y", "watchdog"],
        ["sudo", "-n", "systemctl", "enable", "watchdog.service"],
        ["sudo", "-n", "systemctl", "start", "watchdog.service"],
        skip_condition=are_packages_installed_check("watchdog"),
    )


def git_bb(frontend: UIFrontend):
    with frontend.run_step("git better branch"):
        local_bin = pathlib.Path.home() / ".local" / "bin"

        if not local_bin.exists():
            local_bin.mkdir(parents=True, exist_ok=True)

        script_dst = local_bin / "better-git-branch.sh"

        if script_dst.exists():
            raise StepSkipped()

        script_dst.write_text(better_branch_script)
        script_dst.chmod(0o775)

        subprocess.run(
            ["git", "config", "--global", "alias.bb", f"!{script_dst}"],
            capture_output=True,
            check=True,
        )


def git(frontend: UIFrontend):
    frontend.run_commands(
        "setup git",
        ["git", "config", "--global", "init.defaultBranch", "main"],
        ["git", "config", "--global", "user.name", "Florian Sattler"],
        ["git", "config", "--global", "alias.publish", "push -u origin HEAD"],
    )


def deadsnakes_python(frontend: UIFrontend):
    def skip_condition() -> bool:
        return any(
            i
            for i in pathlib.Path("/etc/apt/sources.list.d/").glob("deadsnakes-ubuntu-ppa-*")
            if i.name.endswith((".list", ".sources"))
        )

    frontend.run_commands(
        "deadsnakes python ppa",
        ["sudo", "-n", "apt-get", "install", "--yes", "software-properties-common"],
        ["sudo", "-n", "add-apt-repository", "--yes", "ppa:deadsnakes/ppa"],
        skip_condition=skip_condition,
    )


def install_docker_and_compose(frontend: UIFrontend):
    frontend.run_script(
        "install docker and docker-compose",
        """curl -fsSL https://get.docker.com | sudo -n sh
        sudo -n usermod -aG docker $USER
        """,
        skip_condition=are_packages_installed_check("docker-ce", "docker-ce-cli", "containerd.io"),
    )


def setup_git_worktree_clone(frontend: UIFrontend):
    shell_function = """
git_worktree_clone() {
    # Check if exactly one argument is provided
    if [ "$#" -ne 1 ]; then
        echo "Usage: git_worktree_clone <REPOSITORY_URL>"
        return 1
    fi

    # Extract the repository name from the URL
    REPO_URL="$1"
    REPO_NAME=$(basename -s .git "$REPO_URL")

    # Clone the repository
    git clone "$REPO_URL" --bare "$REPO_NAME/.git"
    if [ $? -ne 0 ]; then
        echo "Git clone failed"
        return 1
    fi

    # Change directory
    cd "$REPO_NAME/.git" || return

    # Determine the default branch name
    DEFAULT_BRANCH_NAME=$(git ls-remote --symref origin HEAD | grep -oP 'refs/heads/(\\K[^\\s]+)')

    # Add a worktree for the default branch
    git worktree add ../"$DEFAULT_BRANCH_NAME" "$DEFAULT_BRANCH_NAME"
    if [ $? -ne 0 ]; then
        echo "Git worktree add failed"
        return 1
    fi
}
"""

    with frontend.run_step("setup git worktree clone"):
        config = pathlib.Path.home() / ".zshrc"
        config_text = config.read_text()

        if "git_worktree_clone" in config_text:
            raise StepSkipped()

        config.write_text(config_text + shell_function)


def install_google_chrome(frontend: UIFrontend):
    frontend.run_script(
        "install google chrome",
        """
        wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo tee /etc/apt/trusted.gpg.d/google.asc >/dev/null
        echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" | sudo -n tee /etc/apt/sources.list.d/google-chrome.list
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt update -qq
        sudo -n NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt install -y -qq google-chrome-stable
        """,  # noqa: E501
        skip_condition=pathlib.Path("/etc/apt/sources.list.d/google-chrome.list").exists,
    )


def install_firefox(frontend: UIFrontend):
    # check if running ubuntu
    if "ubuntu" in pathlib.Path("/etc/os-release").read_text():
        # install without snap
        frontend.run_script(
            "install firefox",
            """
            sudo -n add-apt-repository -y ppa:mozillateam/ppa
            echo '
Package: *
Pin: release o=LP-PPA-mozillateam
Pin-Priority: 1001

Package: firefox
Pin: version 1:1snap*
Pin-Priority: -1
' | sudo tee /etc/apt/preferences.d/mozilla-firefox

            sudo -n apt update -qq
            sudo -n apt install -y -qq firefox
            """,  # noqa: E501
            skip_condition=is_command_available("firefox"),
        )
    elif "debian" in pathlib.Path("/etc/os-release").read_text():
        frontend.run_script(
            "install firefox",
            """
            sudo -n apt install -y -qq firefox-esr
            """,  # noqa: E501
            skip_condition=is_command_available("firefox"),
        )
    else:
        frontend.run_script(
            "install firefox",
            """
            sudo -n apt install -y -qq firefox
            """,  # noqa: E501
            skip_condition=is_command_available("firefox"),
        )


#
# Invocation
#


def main() -> int:
    set_environment()

    all_steps: typing.Sequence[typing.Callable[[UIFrontend], None]] = [
        check_prerequisites,
        update_system,
        setup_virtual_box_guest_additions,
        setup_regolith_ubuntu_yammy,
        setup_regolith_debian_bookworm,
        setup_regolith_ubuntu_nobel,
        zsh_ohmyzsh,
        update_alias,
        install_docker_and_compose,
        helper_tools,
        vscode,
        devops_ssh,
        watchdog,
        git,
        git_bb,
        setup_git_worktree_clone,
        deadsnakes_python,
        install_google_chrome,
        install_firefox,
    ]

    global ERROR_TEXT

    # get and keep sudo
    get_sudo()

    try:
        with get_frontend() as frontend:
            all_steps = frontend.select_steps(all_steps)
            for step in all_steps:
                step(frontend)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        if ERROR_TEXT is None:
            ERROR_TEXT = ""

        ERROR_TEXT += stringify_exception(e)
        return 1
    finally:
        if ERROR_TEXT:
            print(ERROR_TEXT)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
