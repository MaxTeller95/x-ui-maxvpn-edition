#!/usr/bin/env bash
# ------------------------------------------------------------------------------
#  نصب‌کننده‌ی xui-optimizer  (ابزارها و صفحه‌ی Optimizer برای پنل x-ui / 3x-ui)
# ------------------------------------------------------------------------------
#  روش ۱ - از روی clone:
#      git clone https://github.com/MaxTeller95/xui-optimizer.git
#      cd xui-optimizer && sudo bash install.sh
#
#  روش ۲ - یک خطی (ریپو خصوصی است، توکنی با دسترسی خواندن لازم است):
#      export GITHUB_TOKEN=<token>
#      bash <(curl -fsSL -H "Authorization: token $GITHUB_TOKEN" \
#        https://raw.githubusercontent.com/MaxTeller95/xui-optimizer/main/install.sh)
#
#  گزینه‌ها:
#      (بدون گزینه)       صفحه‌ی Optimizer هم نصب می‌شود، یکپارچه روی پورت اصلی پنل: nginx پورت پنل را
#                         می‌گیرد و x-ui به 127.0.0.1 می‌رود. اگر پنل گواهی ندارد، صفحه رد می‌شود.
#      --no-dash          فقط ابزارها؛ صفحه‌ی Optimizer نصب نشود
#      --dash PORT        صفحه‌ی Optimizer روی پورت دیگری (آزمایشی؛ پنل روی پورت خودش هم می‌ماند)
#      --move             با --dash PORT: PORT باید پورت خود پنل باشد (همان حالت پیش‌فرض)
#      --uninstall        ابزارها، سرویس‌ها و صفحه را حذف کن (تنظیمات و بکاپ‌ها می‌مانند)
#
#  نصب به تنهایی چیزی را در پنل عوض نمی‌کند: هیچ تایمری روشن نمی‌شود و هر ابزار
#  اول تست می‌کند و بکاپ می‌گیرد. اجرای دوباره‌ی همین اسکریپت = به‌روزرسانی.
# ------------------------------------------------------------------------------
set -euo pipefail

REPO=MaxTeller95/xui-optimizer
HOME_DIR=/opt/xui-optimizer
DEST=/usr/local/sbin
TOOLS="xui-optimizer split/xui-split abroad/abroad-tune
       guard/tunnel-guard guard/config-guard guard/xui-path guard/xui-tunnel guard/awg-hop guard/awg-port
       tools/mux-bench tools/xui-fix-inbound-lists tools/xui-probe-client tools/path-score
       tools/xui-reach tools/xui-geo tools/xui-cert tools/xui-subpage"
UNITS="guard/tunnel-guard.service guard/tunnel-guard.timer
       guard/config-guard.service guard/config-guard.path guard/config-guard.timer
       guard/xui-tunnel.service guard/xui-tunnel.timer
       tools/xui-reach.service tools/xui-reach.timer tools/xui-geo.service tools/xui-geo.timer
       tools/xui-subpage.service tools/xui-subpage.path split/xui-split-tune.service split/xui-split-tune.timer"
TIMERS="tunnel-guard.timer config-guard.path config-guard.timer xui-tunnel.timer xui-reach.timer xui-geo.timer xui-subpage.path xui-split-tune.timer"

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
inf() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[32m✔\033[0m %s\n' "$*"; }

DASH_PORT="" MOVE="" UNINSTALL="" NO_DASH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dash)      DASH_PORT=${2:?"بعد از --dash شماره‌ی پورت را بنویسید"}; shift ;;
    --move)      MOVE=move ;;
    --no-dash)   NO_DASH=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   sed -n '2,28p' "$0"; exit 0 ;;
    *)           red "گزینه‌ی ناشناخته: $1"; exit 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { red "این اسکریپت را با root اجرا کنید (sudo bash install.sh)"; exit 1; }

# --------------------------------------------------------------------- حذف
if [ -n "$UNINSTALL" ]; then
  inf "خاموش کردن تایمرها و سرویس‌ها"
  [ -f /usr/local/share/xui-optimizer/subpage/index.html ] && { "$DEST/xui-subpage" off || true; }
  rm -rf /usr/local/share/xui-optimizer /usr/local/lib/xui-optimizer
  rm -f "$DEST/xui-db"
  for t in $TIMERS xo-dash.service; do systemctl disable --now "$t" 2>/dev/null || true; done
  for f in $TOOLS; do rm -f "$DEST/$(basename "$f")"; done
  for u in $UNITS; do rm -f "/etc/systemd/system/$(basename "$u")"; done
  rm -f /etc/systemd/system/xo-dash.service
  if [ -f /etc/nginx/conf.d/xo-dash.conf ]; then
    rm -f /etc/nginx/conf.d/xo-dash.conf
    nginx -t 2>/dev/null && systemctl reload nginx || true
  fi
  rm -rf /opt/xo-dash
  systemctl daemon-reload
  ok "حذف شد. این‌ها دست نخوردند: /etc/tunnel-guard.json، بکاپ‌های /root/xui-backup-* و $HOME_DIR"
  echo "تغییراتی که ابزارها در پنل داده‌اند (قانون‌های مسیریابی، کش DNS و ...) در خود پنل می‌مانند."
  echo "اگر صفحه را با --move نصب کرده بودید، webListen پنل را در تنظیمات پنل خالی کنید و x-ui را ری‌استارت کنید."
  exit 0
fi

# --------------------------------------------------------------------- پیش‌نیازها
need=""
for p in git python3 curl sqlite3; do command -v "$p" >/dev/null || need="$need $p"; done
[ -z "$NO_DASH" ] && ! command -v nginx >/dev/null && need="$need nginx"
if [ -n "$need" ]; then
  inf "نصب پیش‌نیازها:$need"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need >/dev/null
fi

# --------------------------------------------------------------------- ۱. گرفتن سورس
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/guard/tunnel-guard" ] && [ -f "$here/dash/xo-dash" ]; then
  if [ "$here" != "$HOME_DIR" ]; then
    inf "کپی $here به $HOME_DIR"
    mkdir -p "$HOME_DIR" && cp -a "$here"/. "$HOME_DIR"/
  fi
else
  url="https://github.com/$REPO.git"
  [ -n "${GITHUB_TOKEN:-}" ] && url="https://x-access-token:${GITHUB_TOKEN}@github.com/$REPO.git"
  url=${XUI_OPTIMIZER_REPO_URL:-$url}
  if [ -d "$HOME_DIR/.git" ]; then
    inf "به‌روزرسانی $HOME_DIR"
    git -C "$HOME_DIR" pull -q --ff-only "$url" main
  else
    inf "دریافت ریپو در $HOME_DIR"
    git clone -q -b main "$url" "$HOME_DIR" || { red "clone نشد - ریپو خصوصی است؟ GITHUB_TOKEN را تنظیم کنید"; exit 1; }
  fi
  git -C "$HOME_DIR" remote set-url origin "https://github.com/$REPO.git"   # توکن روی دیسک نماند
fi
SRC=$HOME_DIR

# --------------------------------------------------------------------- دیتابیس پنل
# همان‌طور که خود x-ui می‌بیند: محیط پروسه‌ی در حال اجرا، بعد تنظیمات سرویس، بعد SQLite پیش‌فرض
DBINFO=$(python3 "$SRC/lib/xuidb.py" detect 2>&1 || true)
inf "دیتابیس پنل: $DBINFO"
case "$DBINFO" in
  postgres*)
    if ! command -v psql >/dev/null || ! command -v pg_dump >/dev/null; then
      inf "پنل روی PostgreSQL است؛ نصب psql و pg_dump"
      apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-client >/dev/null
    fi ;;
  sqlite*)
    python3 -c "import sys; sys.path.insert(0, '$SRC/lib'); import xuidb; sys.exit(0 if xuidb.exists() else 1)" ||
      red "هشدار: دیتابیس پنل پیدا نشد - اول پنل x-ui یا 3x-ui را نصب کنید." ;;
esac

# --------------------------------------------------------------------- ۲. ابزارها و سرویس‌ها
install -d /usr/local/lib/xui-optimizer                      # ماژول‌های مشترک: دیتابیس (SQLite/PostgreSQL) و ترکیب تانل‌ها
install -m 644 "$SRC"/lib/*.py /usr/local/lib/xui-optimizer/
chmod 755 /usr/local/lib/xui-optimizer/xuidb.py
ln -sf /usr/local/lib/xui-optimizer/xuidb.py "$DEST/xui-db"
for f in $TOOLS; do install -m 755 "$SRC/$f" "$DEST/$(basename "$f")"; done
for u in $UNITS; do install -m 644 "$SRC/$u" "/etc/systemd/system/$(basename "$u")"; done
[ -f /etc/tunnel-guard.json ] || install -m 644 "$SRC/guard/tunnel-guard.example.json" /etc/tunnel-guard.json
systemctl daemon-reload
ok "ابزارها در $DEST نصب شدند: $(for f in $TOOLS; do printf '%s ' "$(basename "$f")"; done)"

# تایمرهایی که قبلاً روشن بوده‌اند با نسخه‌ی جدید دوباره راه می‌افتند
for t in $TIMERS; do
  systemctl is-enabled -q "$t" 2>/dev/null && systemctl restart "$t" && inf "$t با نسخه‌ی جدید ادامه می‌دهد"
done

# صفحه‌ی فارسی اشتراک (xui-subpage on): اگر روشن است، با نسخه‌ی جدید دوباره ساخته شود
if [ -f /usr/local/share/xui-optimizer/subpage/index.html ]; then
  "$DEST/xui-subpage" refresh >/dev/null && ok "صفحه‌ی فارسی اشتراک به‌روز شد"
fi

# --------------------------------------------------------------------- ۳. صفحه‌ی Optimizer
if [ -n "$DASH_PORT" ]; then
  inf "نصب صفحه‌ی Optimizer روی پورت $DASH_PORT ${MOVE:+(گرفتن پورت پنل)}"
  bash "$SRC/dash/install-dash.sh" "$DASH_PORT" ${MOVE:-side}
  ok "صفحه‌ی Optimizer نصب شد"
elif [ -f /opt/xo-dash/xo-dash ]; then          # اگر از قبل نصب بوده، فقط به‌روز شود
  install -m 755 "$SRC/dash/xo-dash" /opt/xo-dash/xo-dash
  install -m 644 "$SRC/dash/index.html" "$SRC/dash/inject.js" /opt/xo-dash/
  systemctl restart xo-dash.service && ok "صفحه‌ی Optimizer به‌روز شد"
elif [ -z "$NO_DASH" ]; then                    # پیش‌فرض: یکپارچه روی پورت اصلی پنل
  PANEL_PORT=$(python3 -c "import sys; sys.path[:0] = ['$SRC/lib']; import xuidb; r = xuidb.connect().execute('select value from settings where key=?', ('webPort',)).fetchone(); print(r[0] if r else '')" 2>/dev/null || true)
  if [ -z "$PANEL_PORT" ]; then
    red "پورت پنل پیدا نشد؛ صفحه‌ی Optimizer نصب نشد. بعد از نصب پنل دوباره اجرا کنید: sudo bash $SRC/install.sh"
  else
    inf "نصب صفحه‌ی Optimizer روی پورت اصلی پنل ($PANEL_PORT)"
    if bash "$SRC/dash/install-dash.sh" "$PANEL_PORT" move; then
      ok "صفحه‌ی Optimizer در منوی پنل نصب شد"
    else
      red "صفحه‌ی Optimizer نصب نشد (پنل گواهی TLS دارد؟). ابزارها نصب هستند؛ برای نصب بدون صفحه: --no-dash"
    fi
  fi
fi

ver=$(git -C "$SRC" log -1 --format='%h %cs' 2>/dev/null || echo "?")
cat <<EOF

xui-optimizer ($ver) نصب شد. قدم‌های بعدی (هر ابزار اول تست می‌کند و بکاپ می‌گیرد):

  xui-optimizer --dry-run && xui-optimizer        # کش DNS و بستن تبلیغات
  xui-split && xui-split apply                    # تقسیم ترافیک سبک/سنگین، آماده‌سازی نگهبان
  cp $SRC/sysctl/99-proxy-tcp.conf /etc/sysctl.d/ && sysctl -p /etc/sysctl.d/99-proxy-tcp.conf
  nano /etc/tunnel-guard.json && tunnel-guard --check && systemctl enable --now tunnel-guard.timer
  config-guard && systemctl enable --now config-guard.path config-guard.timer
  systemctl enable --now xui-tunnel.timer         # جابجایی خودکار بین تانل‌های یک سرور
  systemctl enable --now xui-reach.timer          # دسترسی هاب از داخل ایران، هر ۱۰ دقیقه
  xui-geo update --restart && systemctl enable --now xui-geo.timer   # فهرست‌های ایران، هفتگی
  xui-cert status                                 # گواهی‌ها؛ تمدید خودکار: xui-cert auto DOMAIN --dns dns_arvan

  صفحه‌ی Optimizer: پیش‌فرض نصب می‌شود (یکپارچه روی پورت پنل)؛ بدون آن: sudo bash $SRC/install.sh --no-dash
  به‌روزرسانی: همین اسکریپت را دوباره اجرا کنید.   حذف: sudo bash $SRC/install.sh --uninstall
EOF
