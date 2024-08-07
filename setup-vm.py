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

ERROR_TEXT: str | None = None


class StepFailure(Exception):
    pass


class StepSkipped(Exception):
    pass


class SimpleCLIFrontend:
    @contextlib.contextmanager
    def run_step(self, name: str) -> typing.Generator[None, None, None]:
        try:
            print(name, end=" ", flush=True)
            yield

        except StepSkipped:
            print("─")

        except Exception as e:
            print("✗")
            global ERROR_TEXT
            ERROR_TEXT = stringify_exception(e)
            raise SystemExit(1)

        else:
            print("✓")

    def stop(self) -> None:
        pass

    def select_steps(self, steps: typing.Sequence[typing.Callable]) -> typing.Sequence[typing.Callable]:
        return steps


#
# Curses frontend
#


def draw_line(stdscr: window, line, step_is_selected: bool, step_is_active: bool, step_name: str):
    stdscr.move(line, 0)

    if step_is_selected:
        stdscr.addstr("▶", curses.color_pair(3))
    else:
        stdscr.addstr(" ")

    if step_is_active:
        stdscr.addstr(" ✔ " if step_is_active else " ✗ ", curses.color_pair(1))
    else:
        stdscr.addstr(" ✗ ", curses.color_pair(2))

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
            stdscr.addstr(i, 0, f"✓ {name}")
        elif state == StepState.FAILURE:
            stdscr.addstr(i, 0, f"✗ {name}")
        elif state == StepState.SKIPPED:
            stdscr.addstr(i, 0, f"─ {name}")
        elif state == StepState.RUNNING:
            stdscr.addstr(i, 0, f"{spinner_char} {name}")

    stdscr.refresh()


def stringify_lines(lines: list[tuple[str, StepState]]) -> str:
    icon = {
        StepState.SUCCESS: "✓",
        StepState.FAILURE: "✗",
        StepState.SKIPPED: "─",
        StepState.RUNNING: "▶",
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


class UIFrontend(typing.Protocol):
    def run_step(self, name: str) -> typing.ContextManager[None]: ...

    def stop(self) -> None: ...

    def select_steps(
        self, steps: typing.Sequence[typing.Callable[["typing.Self"], None]]
    ) -> typing.Sequence[typing.Callable[["typing.Self"], None]]: ...


@contextlib.contextmanager
def get_frontend() -> typing.Generator[UIFrontend, None, None]:
    import argparse

    parser = argparse.ArgumentParser(description="Setup VM")
    parser.add_argument("--unattended", action="store_true", help="Run without user interaction")

    if parser.parse_args().unattended or not sys.stdout.isatty():
        yield SimpleCLIFrontend()
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


def run_commands(
    frontend: UIFrontend,
    titel: str,
    *commands: list[str],
    skip_condition: typing.Callable[[], bool] | None = None,
) -> None:
    with frontend.run_step(titel):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        for command in commands:
            subprocess.run(command, capture_output=True, check=True)


def run_script(
    frontend: UIFrontend,
    title: str,
    script: str,
    *,
    skip_condition: typing.Callable[[], bool] | None = None,
) -> None:
    with frontend.run_step(title):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        subprocess.run(
            script,
            shell=True,
            check=True,
            capture_output=True,
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

    with frontend.run_step("locate wget"):
        if shutil.which("wget") is None:
            raise StepFailure()

    with frontend.run_step("locate gpg"):
        if shutil.which("gpg") is None:
            raise StepFailure()


def update_system(frontend: UIFrontend):
    run_commands(
        frontend,
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

    run_script(
        frontend,
        "Setup Regolith",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo -n tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-ubuntu-jammy-amd64 jammy main" | sudo -n tee /etc/apt/sources.list.d/regolith.list
        sudo -n apt update -qq
        sudo -n apt install -y -qq regolith-system-ubuntu
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

    run_script(
        frontend,
        "Setup Regolith",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo -n tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-3_1-debian-bookworm-amd64 bookworm main" | sudo -n tee /etc/apt/sources.list.d/regolith.list
        sudo -n apt update -qq
        sudo -n apt install -y -qq regolith-desktop regolith-session-flashback regolith-look-lascaille regolith-lightdm-config lightdm
        echo "wm.gaps.focus_follows_mouse: true" >> ~/.config/regolith3/Xresources
        """,  # noqa: E501
        skip_condition=skip_condition,
    )


def setup_virtual_box_guest_additions(frontend):
    def get_username():
        import pwd  # after unix check is done

        return pwd.getpwuid(os.getuid())[0]

    run_commands(
        frontend,
        "Virtualbox Dependencies",
        ["sudo", "-n", "apt", "install", "-y", "-qq", "dkms", "gcc", "perl"],
        skip_condition=are_packages_installed_check("dkms", "gcc", "perl"),
    )

    run_script(
        frontend,
        "Virtualbox Guest Addtions",
        f"""
        cd /media/{get_username()}/VBox_GAs_*/
        sudo -n ./VBoxLinuxAdditions.run --quiet
        """,
        skip_condition=is_kernel_module_loaded("vboxguest"),
    )


def zsh_ohmyzsh(frontend: UIFrontend):
    run_script(
        frontend,
        "zsh & ohmyzsh",
        """
        sudo -n apt-get install -y -qq zsh git fzf
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

    run_commands(
        frontend,
        "Helper Tools",
        ["sudo", "-n", "apt", "install", "-y", "-qq", *tools],
        skip_condition=are_packages_installed_check(*tools),
    )


def vscode(frontend: UIFrontend) -> None:
    run_script(
        frontend,
        "vs code",
        """
        wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor >packages.microsoft.gpg
        sudo -n install -D -o root -g root -m 644 packages.microsoft.gpg /etc/apt/keyrings/packages.microsoft.gpg
        sudo -n sh -c 'echo "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" > /etc/apt/sources.list.d/vscode.list'
        rm -f packages.microsoft.gpg

        sudo -n apt install -qq -y apt-transport-https
        sudo -n apt update -qq
        sudo -n apt install -qq -y code
        """,  # noqa: E501
        skip_condition=pathlib.Path("/etc/apt/sources.list.d/vscode.list").exists,
    )


def devops_ssh(frontend: UIFrontend):
    def skip_condition() -> bool:
        config = pathlib.Path.home() / ".ssh/config"

        return config.exists() or "Host ssh.dev.azure.com" in config.read_text()

    run_script(
        frontend,
        "Azure Devops",
        """
        if [ ! -d "$HOME/.ssh" ]; then
            mkdir $HOME/.ssh
            chmod 700 $HOME/.ssh
        fi

        cat <<'EOF' >>~/.ssh/config
        Host ssh.dev.azure.com
            User git
            PubkeyAcceptedAlgorithms +ssh-rsa
            HostkeyAlgorithms +ssh-rsa
        EOF
        """,
        skip_condition=skip_condition,
    )


def watchdog(frontend: UIFrontend):
    run_commands(
        frontend,
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
    run_commands(
        frontend,
        "setup git",
        ["git", "config", "--global", "init.defaultBranch", "main"],
        ["git", "config", "--global", "user.name", "Florian Sattler"],
        ["git", "config", "--global", "alias.publish", "push -u origin HEAD"],
    )


def deadsnakes_python(frontend: UIFrontend):
    def skip_condition() -> bool:
        return any(pathlib.Path("/etc/apt/sources.list.d/").glob("deadsnakes-ubuntu-ppa-*.list"))

    run_commands(
        frontend,
        "deadsnakes python ppa",
        ["sudo", "-n", "apt-get", "install", "--yes", "software-properties-common"],
        ["sudo", "-n", "add-apt-repository", "--yes", "ppa:deadsnakes/ppa"],
        skip_condition=skip_condition,
    )


def install_docker_and_compose(frontend: UIFrontend):
    run_script(
        frontend,
        "install docker and docker-compose",
        "curl -fsSL https://get.docker.com | sudo -n sh",
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


#
# Invocation
#


def main() -> int:
    all_steps: typing.Sequence[typing.Callable[[UIFrontend], None]] = [
        check_prerequisites,
        update_system,
        setup_regolith_ubuntu_yammy,
        setup_regolith_debian_bookworm,
        setup_virtual_box_guest_additions,
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
