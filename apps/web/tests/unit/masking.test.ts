import { describe, expect, it } from "vitest";
import { formatPaise, isUuid, maskOrderRef, truncateId } from "../../src/lib/masking";

describe("masking", () => {
  it("test_format_paise_renders_rupees_from_integer_paise", () => {
    expect(formatPaise(100000)).toBe("₹1,000.00");
    expect(formatPaise(0)).toBe("₹0.00");
    expect(formatPaise(1)).toBe("₹0.01");
  });

  it("test_truncate_id_shows_first_eight_chars_only", () => {
    const id = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
    const truncated = truncateId(id);
    expect(truncated).toBe("a1b2c3d4…");
    expect(truncated).not.toContain("e5f6");
  });

  it("test_mask_order_ref_keeps_prefix_and_last_four_only", () => {
    const masked = maskOrderRef("demo-3f2a9c1b");
    expect(masked.startsWith("demo-")).toBe(true);
    expect(masked.endsWith("9c1b")).toBe(true);
    expect(masked).not.toContain("3f2a");
  });

  it("test_is_uuid_rejects_non_uuids", () => {
    expect(isUuid("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")).toBe(true);
    expect(isUuid("not-a-uuid")).toBe(false);
    expect(isUuid("")).toBe(false);
    expect(isUuid("a1b2c3d4e5f64a7b8c9d0e1f2a3b4c5d")).toBe(false);
  });
});
