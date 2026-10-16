import test from "node:test";
import assert from "node:assert/strict";
import {
  projectField,
  fieldSelection,
  engineeringUpload,
} from "../.test-build/components/field-mapping.js";

const point = (node_id, coordinate, amplitude, real = amplitude, imag = 0) => ({
  node_id,
  coordinate,
  value: { amplitude, real, imag },
});

test("field projections use supplied coordinates, preserve spatial aspect and signed measurements", () => {
  const points = [point(9, [10, 20, 30], 2, -2), point(2, [12, 24, 38], 4)];
  const xy = projectField(points, "XY", "real");
  assert.deepEqual(xy.horizontal, [10, 12]);
  assert.deepEqual(xy.vertical, [20, 24]);
  assert.equal(xy.points[0].value, -2);
  assert.equal(xy.points[1].value, 4);
  assert.equal(
    Math.abs(xy.points[1].x - xy.points[0].x) /
      Math.abs(xy.points[1].y - xy.points[0].y),
    0.5,
  );
  const xz = projectField(points, "XZ", "amplitude");
  assert.deepEqual(xz.vertical, [30, 38]);
  assert.deepEqual(projectField(points, "YZ", "imag").horizontal, [20, 24]);
  assert.equal(xy.points[0].node_id, 9);
});

test("coincident geometry stays finite without invented displacement; missing phase and invalid samples rejected", () => {
  const projected = projectField([point(0, [0, 0, 0], 0)], "XY", "amplitude");
  assert.equal(projected.points[0].x, 320);
  assert.equal(projected.points[0].y, 180);
  assert.deepEqual(projected.horizontal, [0, 0]);
  assert.throws(() =>
    projectField([point(0, [0, NaN, 0], 1)], "XY", "amplitude"),
  );
  assert.throws(() =>
    projectField([point(0, [0, 0, 0], 1, null, null)], "XY", "real"),
  );
  assert.throws(() =>
    projectField(
      Array.from({ length: 513 }, (_, i) => point(i, [i, 0, 0], i)),
      "XY",
      "amplitude",
    ),
  );
  assert.throws(() =>
    projectField(
      [point(0, [0, 0, 0], 1), point(0, [1, 0, 0], 2)],
      "XY",
      "real",
    ),
  );
});

test("field selection requires a frozen model and available terminal payload", () => {
  assert.equal(
    fieldSelection({ status: "PARTIAL", engineering_model: { id: "eng_a" } }),
    true,
  );
  for (const value of [
    { status: "SUCCEEDED" },
    { status: "RUNNING", engineering_model: { id: "eng_a" } },
    {
      status: "SUCCEEDED",
      engineering_model: { id: "eng_a" },
      artifacts_state: "EXPIRED",
    },
  ])
    assert.equal(fieldSelection(value), false);
});

test("engineering upload requires explicit authorization and exact original matrix hash", () => {
  const hash = "a".repeat(64);
  assert.throws(() => engineeringUpload("{}", hash, false));
  assert.throws(() => engineeringUpload("{}", hash, true));
  assert.throws(() =>
    engineeringUpload(
      JSON.stringify({ matrix_sha256: "b".repeat(64) }),
      hash,
      true,
    ),
  );
  const source = {
    matrix_sha256: hash,
    model_version: "v1",
    coordinates: [[0, 1, 2]],
  };
  const parsed = engineeringUpload(JSON.stringify(source), hash, true);
  assert.equal(parsed.manifest.authorized, true);
  assert.equal(parsed.manifest.model_version, "v1");
  assert.deepEqual(parsed.manifest.coordinates, source.coordinates);
  assert.throws(() =>
    engineeringUpload("x".repeat(16 * 1024 ** 2 + 1), hash, true),
  );
});

test("a supplied collinear mesh retains distance along its nonconstant axis", () => {
  const projected = projectField(
    [point(0, [0, 0, 0], 1), point(1, [2, 0, 0], 2)],
    "XY",
    "amplitude",
  );
  assert.ok(projected.points[1].x > projected.points[0].x);
  assert.equal(projected.points[0].y, projected.points[1].y);
});
