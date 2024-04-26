import contextlib
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


class StepFailure(Exception):
    pass


class StepSkipped(Exception):
    pass


class CliFrontend:
    def __init__(self) -> None:
        self.errors_occured = False
        self.busy = False
        self.delay = 0.05

    def spinner(self):
        for c in itertools.cycle(("⣿⣷", "⣿⣯", "⣿⣟", "⣿⡿", "⣿⢿", "⡿⣿", "⢿⣿", "⣻⣿", "⣽⣿", "⣾⣿", "⣷⣿", "⣿⣾")):
            sys.stdout.write(c + " ")
            sys.stdout.flush()
            time.sleep(self.delay)
            sys.stdout.write("\b\b\b")
            sys.stdout.flush()

            if not self.busy:
                break

    def __call__(self, name: str):
        sys.stdout.write(name)
        return self

    def __enter__(self):
        self.busy = True
        sys.stdout.write(" ")
        threading.Thread(target=self.spinner).start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        del exc_val
        del exc_tb

        self.busy = False
        time.sleep(self.delay * 1.5)
        status = "✓ \n" if exc_type is None else "─ \n" if exc_type == StepSkipped else "✗ \n"
        sys.stdout.write(status)
        sys.stdout.flush()

        if exc_type == StepSkipped:
            return True


frontend = CliFrontend()

#
# Step Helper
#


def get_sudo() -> int:
    try:
        subprocess.run(["sudo", "true"], check=True)
    except subprocess.CalledProcessError as e:
        return e.returncode

    else:
        return 0


@contextlib.contextmanager
def log_subprocess_error():
    try:
        yield

    except subprocess.CalledProcessError as e:
        sys.stderr.buffer.write(e.stderr)
        raise

    except Exception as e:
        sys.stderr.write(str(e))
        raise


def run_commands(
    titel: str,
    *commands: list[str],
    skip_condition: typing.Callable[[], bool] | None = None,
    need_sudo: bool = True,
) -> None:
    if need_sudo:
        get_sudo()

    with frontend(titel):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        with log_subprocess_error():
            for command in commands:
                result = subprocess.run(command, capture_output=True)
                result.check_returncode()


def run_script(
    title: str,
    script: str,
    *,
    skip_condition: typing.Callable[[], bool] | None = None,
    need_sudo: bool = True,
) -> None:
    if need_sudo:
        get_sudo()

    with frontend(title):
        if skip_condition is not None:
            if skip_condition():
                raise StepSkipped()

        with log_subprocess_error():
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


def check_prerequisites() -> None:
    with frontend("64-bit Linux"):
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise StepFailure()

    with frontend("don't run as root"):
        if os.getuid() == 0:
            raise StepFailure()

    with frontend("locate apt"):
        if shutil.which("apt") is None:
            raise StepFailure()

    with frontend("locate wget"):
        if shutil.which("wget") is None:
            raise StepFailure()

    with frontend("locate gpg"):
        if shutil.which("gpg") is None:
            raise StepFailure()


def update_system():
    run_commands(
        "Update System",
        ["sudo", "-n", "apt", "update", "-qq"],
        ["sudo", "-n", "apt", "full-upgrade", "--auto-remove", "-y", "--purge", "-qq"],
    )


def setup_regolith_yammy() -> None:
    def skip_condition() -> bool:
        releases_path = pathlib.Path("/etc/os-release")
        return (
            not releases_path.exists()
            or "UBUNTU_CODENAME=jammy" not in releases_path.read_text()
            or pathlib.Path("/etc/apt/sources.list.d/regolith.list").exists()
        )

    run_script(
        "Setup Regolith",
        """
        wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo -n tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
        echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-ubuntu-jammy-amd64 jammy main" | sudo -n tee /etc/apt/sources.list.d/regolith.list
        sudo -n apt update -qq
        sudo -n apt install -y -qq regolith-system-ubuntu
        """,  # noqa: E501
        skip_condition=skip_condition,
    )


def setup_virtual_box_guest_additions():
    def get_username():
        import pwd  # after unix check is done

        return pwd.getpwuid(os.getuid())[0]

    run_commands(
        "Virtualbox Dependencies",
        ["sudo", "-n", "apt", "install", "-y", "-qq", "dkms", "gcc", "perl"],
        skip_condition=are_packages_installed_check("dkms", "gcc", "perl"),
    )

    run_script(
        "Virtualbox Guest Addtions",
        f"""
        cd /media/{get_username()}/VBox_GAs_*/
        sudo -n ./VBoxLinuxAdditions.run --quiet
        """,
        skip_condition=is_kernel_module_loaded("vboxguest"),
    )


def zsh_ohmyzsh():
    run_script(
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


def update_alias():
    with frontend("alias up"):
        config = pathlib.Path.home() / ".zshrc"
        config_text = config.read_text()

        if "alias up=" in config_text:
            raise StepSkipped()

        config.write_text(config_text + "\nalias up='sudo apt update && sudo apt full-upgrade --auto-remove -y'\n")


def helper_tools():
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
        "Helper Tools",
        ["sudo", "-n", "apt", "install", "-y", "-qq", *tools],
        skip_condition=are_packages_installed_check(*tools),
    )


def vscode() -> None:
    run_script(
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


def devops_ssh():
    def skip_condition() -> bool:
        config = pathlib.Path.home() / ".ssh/config"

        return config.exists() or "Host ssh.dev.azure.com" in config.read_text()

    run_script(
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


def watchdog():
    run_commands(
        "install watchdog",
        ["sudo", "-n", "apt", "install", "-qq", "-y", "watchdog"],
        ["sudo", "-n", "systemctl", "enable", "watchdog.service"],
        ["sudo", "-n", "systemctl", "start", "watchdog.service"],
        skip_condition=are_packages_installed_check("watchdog"),
    )


def git_bb():
    with frontend("git better branch"):
        local_bin = pathlib.Path.home() / ".local" / "bin"

        if not local_bin.exists():
            local_bin.mkdir(parents=True, exist_ok=True)

        script_dst = local_bin / "better-git-branch.sh"

        if script_dst.exists():
            raise StepSkipped()

        script_dst.write_text(better_branch_script)
        script_dst.chmod(0o775)

        with log_subprocess_error():
            subprocess.run(
                ["git", "config", "--global", "alias.bb", f"!{script_dst}"],
                capture_output=True,
                check=True,
            )


def git():
    run_commands(
        "setup git",
        ["git", "config", "--global", "init.defaultBranch", "main"],
        ["git", "config", "--global", "user.name", "Florian Sattler"],
    )


def deadsnakes_python():
    def skip_condition() -> bool:
        return any(pathlib.Path("/etc/apt/sources.list.d/").glob("deadsnakes-ubuntu-ppa-*.list"))

    run_commands(
        "deadsnakes python ppa",
        ["sudo", "-n", "add-apt-repository", "--yes", "ppa:deadsnakes/ppa"],
        skip_condition=skip_condition,
    )


#
# Invocation
#


def main() -> int:
    try:
        check_prerequisites()
        get_sudo()
        update_system()
        setup_regolith_yammy()
        setup_virtual_box_guest_additions()
        zsh_ohmyzsh()
        update_alias()
        helper_tools()
        vscode()
        devops_ssh()
        watchdog()
        git()
        git_bb()
        deadsnakes_python()
    except Exception as e:
        if str(e):
            print(e)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
