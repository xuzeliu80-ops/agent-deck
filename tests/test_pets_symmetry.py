import json, unittest
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parents[1] / "aw" / "pets_pixel.json").read_text(encoding="utf-8"))


class PetSymmetryTest(unittest.TestCase):
    def test_every_frame_is_14x12_and_mirror_symmetric(self):
        bad = []
        for pet, states in DATA["frames"].items():
            for state, frames in states.items():
                for i, frame in enumerate(frames):
                    self.assertEqual(len(frame), 12, (pet, state, i))
                    for y, row in enumerate(frame):
                        self.assertEqual(len(row), 14, (pet, state, i, y))
                        if row != row[::-1]:
                            bad.append(f"{pet}.{state}[{i}] row{y}: {row}")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
