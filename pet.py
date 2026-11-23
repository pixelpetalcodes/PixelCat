import random
import sys
import time
from pathlib import Path

import cv2
from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPixmap, QTransform
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import QApplication, QWidget

# pyright: reportCallIssue=false, reportAttributeAccessIssue=false

# ---------------- SETTINGS (edit these to match your setup) ----------------
SHEET = Path("sprites/cat.png")   # sprite sheet; if missing, a placeholder cat is drawn
FRAME_W, FRAME_H = 32, 32         # size of ONE frame in the sheet, in pixels
SCALE = 5                         # how much to enlarge the pixel art
FPS = 8                           # animation speed (frames per second)
SPEED = 3                         # walking speed in pixels per tick

USE_WEBCAM = True                 # False = the cat just wanders randomly
CAMERA_INDEX = 0                  # 0 = default webcam; try 1 if the wrong camera opens
ABSENT_SECONDS = 6                # no face for this long -> cat falls asleep

SOUND_ON = True                   # False = silent cat
VOLUME = 0.5                      # 0.0 (mute) to 1.0 (loud)
SOUNDS = {                        # WAV files only
    # every name starting with "meow" is part of the random meow pool
    "meow_attention": "sounds/meow_attention.wav",
    "meow_little": "sounds/meow_little.wav",
    "meow_sweet": "sounds/meow_sweet.wav",
    "meow_begging": "sounds/meow_begging.wav",
    # every name starting with "scared" is part of the random scared pool
    "scared_yelp": "sounds/yelp.wav",
    "scared_angry": "sounds/angry.wav",
    "purr": "sounds/purr.wav",
}
SOUND_COOLDOWN = {"meow": 1.0, "scared": 1.5, "purr": 20.0}   # seconds between plays
IDLE_MEOW_CHANCE = 0.004          # chance per tick that a sitting cat meows (0 = never)

# animation name -> (row number in the sheet starting at 0, number of frames)
ANIMS = {
    "idle": (0, 4),
    "walk": (4, 8),
    "sleep": (6, 4),
    "paw": (7, 6),
    "jump": (8, 7),
    "scared": (9, 8),
}
# ---------------------------------------------------------------------------


class FaceWatcher(QThread):
    """Reads the webcam in the background and reports where your face is.
    Nothing is saved or sent anywhere: frames are looked at and thrown away."""

    seen = pyqtSignal(float, bool)   # (x position 0..1, face present?)
    failed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.running = True

    def run(self):
        cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print("Could not open the webcam. Check camera permissions in Windows settings.")
            self.failed.emit()
            return
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        while self.running:
            ok, frame = cap.read()
            if not ok:
                self.msleep(200)
                continue
            width = 320
            height = int(frame.shape[0] * width / frame.shape[1])
            small = cv2.resize(frame, (width, height))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(40, 40))
            if len(faces) > 0:
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx = 1.0 - (x + w / 2) / width      # mirrored: your right = screen right
                self.seen.emit(float(cx), True)
            else:
                self.seen.emit(0.5, False)
            self.msleep(150)
        cap.release()

    def stop(self):
        self.running = False
        self.wait()


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.pw, self.ph = FRAME_W * SCALE, FRAME_H * SCALE
        self.resize(self.pw, self.ph)

        self.sheet = QPixmap(str(SHEET)) if SHEET.exists() else None

        primary = QGuiApplication.primaryScreen()
        assert primary is not None
        screen = primary.availableGeometry()
        self.floor = screen.bottom() - self.ph + 1
        self.min_x = screen.left()
        self.max_x = screen.right() - self.pw
        self.move(screen.left() + 200, self.floor)

        self.state = "idle"
        self.frame = 0
        self.direction = 1          # 1 = facing right, -1 = facing left
        self.ticks = 0
        self.state_len = 30
        self.dragging = False
        self.drag_offset = None

        # sounds
        self.sounds = {}
        self.last_played = {}
        if SOUND_ON:
            for name, path in SOUNDS.items():
                file = Path(path)
                if file.exists():
                    effect = QSoundEffect(self)
                    effect.setSource(QUrl.fromLocalFile(str(file.resolve())))
                    effect.setVolume(VOLUME)
                    self.sounds[name] = effect

        # webcam state
        self.use_webcam = USE_WEBCAM
        self.face_present = False
        self.face_x = 0.5
        self.last_seen = time.time()
        self.watcher = None
        if self.use_webcam:
            self.watcher = FaceWatcher()
            self.watcher.seen.connect(self.on_face)
            self.watcher.failed.connect(self.on_camera_failed)
            self.watcher.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000 // FPS)

    # ----- webcam signals -----
    def on_face(self, x, present):
        self.face_present = present
        if present:
            self.face_x = x
            self.last_seen = time.time()

    def on_camera_failed(self):
        self.use_webcam = False     # fall back to wandering around randomly

    # ----- behaviour -----
    def play(self, name):
        options = [e for k, e in self.sounds.items() if k.startswith(name)]
        effect = random.choice(options) if options else None
        if effect is None:
            return
        now = time.time()
        if now - self.last_played.get(name, 0) < SOUND_COOLDOWN.get(name, 0):
            return
        self.last_played[name] = now
        effect.play()

    def set_state(self, name):
        if name != self.state:
            self.state = name
            self.frame = 0
            self.ticks = 0
            # sounds that go with certain moods
            if name == "jump":
                self.play("meow")
            elif name == "scared":
                self.play("scared")
            elif name == "paw":
                self.play("purr")

    def tick(self):
        self.ticks += 1
        _, count = ANIMS[self.state]
        self.frame = (self.frame + 1) % count

        if not self.dragging:
            if self.use_webcam:
                self.webcam_behavior()
            elif self.ticks >= self.state_len:
                self.pick_new_state()

            if self.state == "walk":
                x = self.x() + SPEED * self.direction
                if x <= self.min_x or x >= self.max_x:
                    self.direction *= -1
                    x = max(self.min_x, min(x, self.max_x))
                self.move(x, self.floor)
        self.update()

    def webcam_behavior(self):
        since = time.time() - self.last_seen
        if self.face_present:
            if self.state == "sleep":
                self.set_state("jump")                  # wakes up excited
            elif self.state == "jump":
                if self.ticks >= 7:
                    self.set_state("idle")
            else:
                target = self.min_x + int(self.face_x * (self.max_x - self.min_x))
                dx = target - self.x()
                if abs(dx) > 40:
                    self.direction = 1 if dx > 0 else -1
                    self.set_state("walk")              # follows you
                elif self.state == "walk":
                    self.set_state("idle")
                elif self.state == "idle" and self.ticks > 24:
                    self.set_state("paw")
                elif self.state == "idle" and random.random() < IDLE_MEOW_CHANCE:
                    self.play("meow")
                elif self.state == "paw" and self.ticks > 12:
                    self.set_state("idle")
        elif since > ABSENT_SECONDS:
            self.set_state("sleep")                     # you left, nap time
        elif self.state == "walk":
            self.set_state("idle")

    def pick_new_state(self):
        self.state = random.choice(["idle", "walk", "walk", "sleep"])
        self.direction = random.choice([1, -1])
        self.frame = 0
        self.ticks = 0
        self.state_len = random.randint(25, 80)

    # ----- drawing -----
    def paintEvent(self, event):
        painter = QPainter(self)
        if self.sheet:
            row, _ = ANIMS[self.state]
            frame = self.sheet.copy(self.frame * FRAME_W, row * FRAME_H, FRAME_W, FRAME_H)
            frame = frame.scaled(
                self.pw, self.ph,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,   # keeps pixels crisp
            )
            if self.direction == -1:
                frame = frame.transformed(QTransform().scale(-1, 1))
            painter.drawPixmap(0, 0, frame)
        else:
            self.draw_placeholder(painter)

    def draw_placeholder(self, p):
        u = SCALE * 2
        body = QColor("#f4a261")
        p.fillRect(2 * u, 3 * u, 4 * u, 2 * u, body)      # body
        p.fillRect(5 * u, 2 * u, 2 * u, 2 * u, body)      # head
        p.fillRect(5 * u, 1 * u, 1 * u, 1 * u, body)      # ears
        p.fillRect(6 * u, 1 * u, 1 * u, 1 * u, body)
        eye = QColor("#222222")
        if self.state == "sleep":
            p.fillRect(5 * u + 2, 2 * u + u // 2, u // 2, 2, eye)
        else:
            p.fillRect(6 * u, 2 * u, 3, 3, eye)

    # ----- mouse: drag with left button, quit with right button -----
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.set_state("scared")
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == Qt.MouseButton.RightButton:
            QApplication.quit()

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            self.move(self.x(), self.floor)   # drop back to the floor
            self.set_state("idle")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    pet = Pet()
    pet.show()
    if pet.watcher is not None:
        app.aboutToQuit.connect(pet.watcher.stop)
    sys.exit(app.exec())