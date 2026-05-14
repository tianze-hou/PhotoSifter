import os
import shutil
import io
import hashlib
import time
import threading
import json
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, send_from_directory, jsonify, request, send_file
from PIL import Image

app = Flask(__name__)

# 配置路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, 'photos')
YES_DIR = os.path.join(BASE_DIR, 'yes')
NO_DIR = os.path.join(BASE_DIR, 'no')
CACHE_DIR = os.path.join(BASE_DIR, '.cache')

# 确保目录存在
for d in [PHOTO_DIR, YES_DIR, NO_DIR, CACHE_DIR]:
    os.makedirs(d, exist_ok=True)

# 图片预览最大尺寸
MAX_PREVIEW_SIZE = (1920, 1080)
# 缩略图尺寸
THUMB_SIZE = (400, 400)

# 内存缓存：状态计数器
class StateCache:
    def __init__(self):
        self.pending = []
        self.pending_set = set()
        self.yes_count = 0
        self.no_count = 0
        self.total = 0
        self._last_refresh = 0
        self._refresh_interval = 0.5
        self._lock = threading.Lock()
        self._mtime_cache = {}
        self._refresh()

    def _dir_mtime(self, path):
        try:
            return os.stat(path).st_mtime
        except Exception:
            return 0

    def _needs_refresh(self):
        for d in [PHOTO_DIR, YES_DIR, NO_DIR]:
            if self._mtime_cache.get(d, 0) != self._dir_mtime(d):
                return True
        return False

    def _refresh(self):
        with self._lock:
            now = time.time()
            if now - self._last_refresh < self._refresh_interval:
                return
            if not self._needs_refresh() and self.total > 0:
                self._last_refresh = now
                return
            self._last_refresh = now
            for d in [PHOTO_DIR, YES_DIR, NO_DIR]:
                self._mtime_cache[d] = self._dir_mtime(d)
            extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff')
            self.pending = sorted([
                f for f in os.listdir(PHOTO_DIR)
                if f.lower().endswith(extensions)
            ])
            self.pending_set = set(self.pending)
            self.yes_count = len(os.listdir(YES_DIR))
            self.no_count = len(os.listdir(NO_DIR))
            self.total = len(self.pending) + self.yes_count + self.no_count

    def get_pending(self):
        self._refresh()
        return self.pending

    def get_status(self):
        self._refresh()
        return {
            'total': self.total,
            'done': self.yes_count + self.no_count,
            'next_photo': self.pending[0] if self.pending else None,
            'pending_count': len(self.pending),
            'pending': self.pending
        }

    def after_move(self):
        with self._lock:
            self._last_refresh = 0
        self._refresh()

    def remove_from_pending(self, filename):
        with self._lock:
            if filename in self.pending_set:
                self.pending_set.discard(filename)
                self.pending = [f for f in self.pending if f != filename]
                self.total -= 1

    def add_to_pending(self, filename):
        with self._lock:
            if filename not in self.pending_set:
                self.pending_set.add(filename)
                self.pending.append(filename)
                self.pending.sort()
                self.total += 1

    def adjust_counts(self, yes_delta=0, no_delta=0):
        with self._lock:
            self.yes_count = max(0, self.yes_count + yes_delta)
            self.no_count = max(0, self.no_count + no_delta)

state_cache = StateCache()

# 历史记录，用于撤销操作 (存储: (filename, source_path, dest_path))
HISTORY_MAX_SIZE = 100
history = []
HISTORY_FILE = os.path.join(BASE_DIR, '.history.json')
_history_lock = threading.Lock()


def _load_history():
    global history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    # 验证数据结构并过滤无效记录
                    valid = []
                    for item in data:
                        if isinstance(item, (list, tuple)) and len(item) == 3:
                            valid.append(tuple(item))
                    history = valid[-HISTORY_MAX_SIZE:]
        except Exception:
            history = []


def _save_history():
    with _history_lock:
        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False)
        except Exception:
            pass


_load_history()


def _get_cache_path(filename, size_name):
    safe_name = hashlib.md5(filename.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f"{safe_name}_{size_name}.webp")


_preview_lock = threading.Lock()
_preview_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview")


def _generate_preview(src_path, dest_path, max_size):
    if os.path.exists(dest_path):
        return dest_path
    with _preview_lock:
        if os.path.exists(dest_path):
            return dest_path
        try:
            with Image.open(src_path) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.thumbnail(max_size, Image.Resampling.BILINEAR)
                img.save(dest_path, 'WEBP', quality=80, method=3)
            return dest_path
        except Exception:
            return src_path


def _generate_preview_async(src_path, dest_path, max_size):
    _preview_executor.submit(_generate_preview, src_path, dest_path, max_size)


def _get_image_preview_path(filename, max_size=MAX_PREVIEW_SIZE):
    src = os.path.join(PHOTO_DIR, filename)
    if not os.path.exists(src):
        return None
    cache_path = _get_cache_path(filename, f"{max_size[0]}x{max_size[1]}")
    return _generate_preview(src, cache_path, max_size)


def _get_image_preview_path_async(filename, max_size=MAX_PREVIEW_SIZE):
    src = os.path.join(PHOTO_DIR, filename)
    if not os.path.exists(src):
        return None
    cache_path = _get_cache_path(filename, f"{max_size[0]}x{max_size[1]}")
    if os.path.exists(cache_path):
        return cache_path
    _generate_preview_async(src, cache_path, max_size)
    return src


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/status')
def status():
    return jsonify(state_cache.get_status())


@app.route('/images/<path:filename>')
def serve_image(filename):
    preview_path = _get_image_preview_path(filename, MAX_PREVIEW_SIZE)
    if preview_path and os.path.exists(preview_path):
        return send_from_directory(
            os.path.dirname(preview_path),
            os.path.basename(preview_path),
            max_age=86400
        )
    return send_from_directory(PHOTO_DIR, filename, max_age=86400)


@app.route('/images/original/<path:filename>')
def serve_original(filename):
    return send_from_directory(PHOTO_DIR, filename, max_age=86400)


@app.route('/move', methods=['POST'])
def move_photo():
    data = request.json
    filename = data.get('filename')
    direction = data.get('direction')

    if not filename:
        return jsonify({'status': 'error', 'message': 'missing filename'}), 400

    src = os.path.join(PHOTO_DIR, filename)
    dest_dir = YES_DIR if direction == 'yes' else NO_DIR
    dest = os.path.join(dest_dir, filename)

    if os.path.exists(src):
        shutil.move(src, dest)
        history.append((filename, src, dest))
        if len(history) > HISTORY_MAX_SIZE:
            history.pop(0)
        _save_history()
        state_cache.remove_from_pending(filename)
        if direction == 'yes':
            state_cache.adjust_counts(yes_delta=1)
        else:
            state_cache.adjust_counts(no_delta=1)
        return jsonify({'status': 'success'})
    return jsonify({'status': 'file not found'}), 404


@app.route('/undo', methods=['POST'])
def undo():
    if not history:
        return jsonify({'status': 'nothing to undo'}), 400

    filename, original_src, current_dest = history.pop()
    _save_history()
    if os.path.exists(current_dest):
        shutil.move(current_dest, original_src)
        state_cache.add_to_pending(filename)
        if current_dest.startswith(YES_DIR):
            state_cache.adjust_counts(yes_delta=-1)
        else:
            state_cache.adjust_counts(no_delta=-1)
        return jsonify({'status': 'success'})
    return jsonify({'status': 'file gone'}), 404


@app.route('/preload/<path:filename>')
def preload_info(filename):
    preview_path = _get_image_preview_path_async(filename, MAX_PREVIEW_SIZE)
    if preview_path and os.path.exists(preview_path):
        return jsonify({
            'url': f'/images/{filename}',
            'cached': True
        })
    return jsonify({
        'url': f'/images/{filename}',
        'cached': False
    })


if __name__ == '__main__':
    print(f"Server running at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
