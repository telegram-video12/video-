#!/data/data/com.termux/files/usr/bin/bash
# Installs "הספרייה שלי" on Android inside Termux.
# Run:  curl -fsSLo i.sh https://raw.githubusercontent.com/telegram-video12/video-/HEAD/install-termux.sh && bash i.sh
set -e
REPO="https://github.com/telegram-video12/video-.git"
DIR="$HOME/tgvideo"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }
# Termux shows Hebrew reversed, so all messages here are in English.

# Keep Android from killing the install while the screen is off / app in background
termux-wake-lock 2>/dev/null || true

say "Step 1/4: Updating Termux (can take a few minutes, keep Termux open!)"
pkg update -y </dev/null
pkg upgrade -y -o Dpkg::Options::=--force-confnew </dev/null
pkg install -y python git openssl </dev/null

say "Step 2/4: Downloading the site"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

say "Step 3/4: Installing components"
pip install -r "$DIR/requirements.txt"

say "Step 4/4: Creating shortcuts"
cat > "$PREFIX/bin/tv" <<CMD
#!/data/data/com.termux/files/usr/bin/bash
# start the site (keeps running while Termux is open)
termux-wake-lock 2>/dev/null || true
cd "$DIR" && exec python -m app
CMD
cat > "$PREFIX/bin/tv-update" <<CMD
#!/data/data/com.termux/files/usr/bin/bash
# download the newest version of the site
cd "$DIR" && git pull --ff-only && pip install -q -r requirements.txt && echo "Updated! Now type: tv"
CMD
chmod +x "$PREFIX/bin/tv" "$PREFIX/bin/tv-update"

termux-wake-unlock 2>/dev/null || true
printf '\n\033[1;32m*** INSTALLATION COMPLETE ***\033[0m\n'
echo "To start the site type:   tv"
echo "To update later type:     tv-update"
