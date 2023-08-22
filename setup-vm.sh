#!/bin/bash
set -euo pipefail

if [ "$UID" -eq 0 ]; then
    echo "Please don't run using root privileges"
    exit
fi

if ! command -v apt &>/dev/null; then
    echo "apt could not be found"
    exit
fi

if ! command -v gpg &>/dev/null; then
    echo "gpg could not be found"
    exit
fi

if ! command -v wget &>/dev/null; then
    echo "wget could not be found"
    exit
fi

set -x

# make sure system is up to date
sudo apt update -qq
sudo apt full-upgrade --auto-remove -y --purge -qq

# Regolith
wget -qO - https://regolith-desktop.org/regolith.key | gpg --dearmor | sudo tee /usr/share/keyrings/regolith-archive-keyring.gpg >/dev/null
echo deb "[arch=amd64 signed-by=/usr/share/keyrings/regolith-archive-keyring.gpg] https://regolith-desktop.org/release-ubuntu-jammy-amd64 jammy main" | sudo tee /etc/apt/sources.list.d/regolith.list
sudo apt update -qq
sudo apt install -y -qq regolith-system-ubuntu

# virtual box dependencies
sudo apt install -y -qq dkms gcc perl

# zsh & ohmyzsh
sudo apt install -y -qq zsh git
sudo chsh -s "$(which zsh)" "$USER"

if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
fi

# utils
if ! grep -Fq "alias up=" ~/.zshrc; then
    echo "alias up='sudo apt update && sudo apt full-upgrade --auto-remove -y'" >>~/.zshrc
fi

# usefull helpers and python dependencies
sudo apt install -y -qq curl htop zlib1g-dev atool arc arj lzip lzop nomarch p7zip-full rar rpm unace unalz unrar libncursesw5-dev libreadline-dev libssl-dev libgdbm-dev libc6-dev libsqlite3-dev libbz2-dev libffi-dev lzma-dev tk-dev liblzma-dev nano

# pyenv

if [ ! -d "$HOME/.pyenv" ]; then
    curl https://pyenv.run | bash
fi

if ! grep -Fq "PYENV_ROOT" ~/.profile; then
    cat <<'EOF' | tee -a ~/.profile ~/.zprofile
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init --path)"
EOF
fi

# vs code
wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor >packages.microsoft.gpg
sudo install -D -o root -g root -m 644 packages.microsoft.gpg /etc/apt/keyrings/packages.microsoft.gpg
sudo sh -c 'echo "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" > /etc/apt/sources.list.d/vscode.list'
rm -f packages.microsoft.gpg

sudo apt install -qq -y apt-transport-https
sudo apt update -qq
sudo apt install -qq -y code

# azure devops

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

# watchdog
sudo apt install -qq -y watchdog
sudo systemctl enable watchdog.service
sudo systemctl start watchdog.service

# git
git config --global init.defaultBranch main
