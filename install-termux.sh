#!/data/data/com.termux/files/usr/bin/bash
# Installs "הספרייה שלי" on Android inside Termux.
# Run:  curl -fsSLo i.sh https://raw.githubusercontent.com/telegram-video12/video-/HEAD/install-termux.sh && bash i.sh
set -e
REPO="https://github.com/telegram-video12/video-.git"
DIR="$HOME/tgvideo"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }

say "שלב 1/4: מעדכן את Termux (יכול לקחת כמה דקות)"
pkg update -y </dev/null
pkg upgrade -y -o Dpkg::Options::=--force-confnew </dev/null
pkg install -y python git openssl </dev/null

say "שלב 2/4: מוריד את האתר"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

say "שלב 3/4: מתקין רכיבים"
pip install -r "$DIR/requirements.txt"

say "שלב 4/4: יוצר פקודות קיצור"
cat > "$PREFIX/bin/tv" <<CMD
#!/data/data/com.termux/files/usr/bin/bash
# start the site (keeps running while Termux is open)
termux-wake-lock 2>/dev/null || true
cd "$DIR" && exec python -m app
CMD
cat > "$PREFIX/bin/tv-update" <<CMD
#!/data/data/com.termux/files/usr/bin/bash
# download the newest version of the site
cd "$DIR" && git pull --ff-only && pip install -q -r requirements.txt && echo "עודכן! עכשיו הקלד: tv"
CMD
chmod +x "$PREFIX/bin/tv" "$PREFIX/bin/tv-update"

printf '\n\033[1;32m✓ ההתקנה הסתיימה!\033[0m\n'
echo "כדי להפעיל את האתר הקלד:  tv"
echo "לעדכון לגרסה חדשה הקלד:   tv-update"
