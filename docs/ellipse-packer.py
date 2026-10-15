import math
import numpy as np
from shapely.geometry import Point
from shapely.affinity import scale, translate
from shapely.ops import unary_union

def create_ellipse_polygon(semi_a, semi_b, clearance=0.0625, num_points=32):
    """
    Creates an axis-aligned ellipse centered at origin (0,0) in inches,
    buffered by half clearance for collision-free overlap checks.
    """
    circle = Point(0, 0).buffer(1.0, resolution=num_points // 4)
    ellipse = scale(circle, xfact=semi_a + (clearance / 2.0), 
                             yfact=semi_b + (clearance / 2.0), 
                             origin=(0, 0))
    return ellipse

class EllipsePacker:
    def __init__(self, plate_width_in, plate_height_in, step_size_in=0.05):
        self.plate_w = plate_width_in
        self.plate_h = plate_height_in
        self.step_size = step_size_in  # Grid resolution in inches

    def pack(self, ellipse_specs, mode="PREFER_WIDTH", clearance=0.0625):
        """
        ellipse_specs: list of dicts [{'semi_a': float, 'semi_b': float, 'id': str/int}] (all in inches)
        clearance: minimum tool spacing/kerf allowance in inches (default 1/16")
        mode: "PREFER_WIDTH" (minimizes total X) or "PREFER_HEIGHT" (minimizes total Y)
        """
        # Sort specs by area descending (largest items pack first)
        sorted_specs = sorted(ellipse_specs, key=lambda s: s['semi_a'] * s['semi_b'], reverse=True)

        packed_results = []
        occupied_region = None  # Union of all placed buffered polygons

        for item in sorted_specs:
            a, b = item['semi_a'], item['semi_b']
            
            # Orientations to try: 0 deg (a, b) and 90 deg (b, a)
            orientations = [(a, b, 0), (b, a, 90)]
            
            best_pos = None
            best_rotation = None
            min_extent = float('inf')

            # Build search grid based on plate dimensions in inches
            if mode == "PREFER_WIDTH":
                x_range = np.arange(0, self.plate_w, self.step_size)
                y_range = np.arange(0, self.plate_h, self.step_size)
            else:
                y_range = np.arange(0, self.plate_h, self.step_size)
                x_range = np.arange(0, self.plate_w, self.step_size)

            found = False
            for semi_x, semi_y, rot_angle in orientations:
                base_poly = create_ellipse_polygon(semi_x, semi_y, clearance)
                bounds = base_poly.bounds  # (minx, miny, maxx, maxy)
                half_w, half_h = (bounds[2] - bounds[0]) / 2.0, (bounds[3] - bounds[1]) / 2.0

                for x in x_range:
                    if x + half_w > self.plate_w or x - half_w < 0:
                        continue
                        
                    for y in y_range:
                        if y + half_h > self.plate_h or y - half_h < 0:
                            continue

                        # Candidate center position
                        candidate = translate(base_poly, xoff=x, yoff=y)

                        # Collision check against already placed items
                        if occupied_region is not None and occupied_region.intersects(candidate):
                            continue

                        # Primary objective penalty
                        if mode == "PREFER_WIDTH":
                            extent = (x + half_w) * 1000 + (y + half_h)
                        else:
                            extent = (y + half_h) * 1000 + (x + half_w)

                        if extent < min_extent:
                            min_extent = extent
                            best_pos = (round(x, 4), round(y, 4))
                            best_rotation = rot_angle
                            best_candidate = candidate
                            found = True

            if found:
                if occupied_region is None:
                    occupied_region = best_candidate
                else:
                    occupied_region = unary_union([occupied_region, best_candidate])

                placed_a = item['semi_a'] if best_rotation == 0 else item['semi_b']
                placed_b = item['semi_b'] if best_rotation == 0 else item['semi_a']

                packed_results.append({
                    'id': item.get('id'),
                    'center_in': best_pos,
                    'rotation_deg': best_rotation,
                    'semi_a_in': placed_a,
                    'semi_b_in': placed_b,
                })
            else:
                print(f"Warning: Could not fit label '{item.get('id')}' on the plate.")

        # Calculate used boundary and salvaged scrap rectangle dimensions
        max_x = max([p['center_in'][0] + p['semi_a_in'] for p in packed_results]) if packed_results else 0.0
        max_y = max([p['center_in'][1] + p['semi_b_in'] for p in packed_results]) if packed_results else 0.0
        
        scrap_info = {
            'used_width_in': round(max_x, 3),
            'used_height_in': round(max_y, 3),
            'salvaged_scrap_rect_in': (
                round(self.plate_w - max_x, 3), round(self.plate_h, 3)
            ) if mode == "PREFER_WIDTH" else (
                round(self.plate_w, 3), round(self.plate_h - max_y, 3)
            )
        }

        return packed_results, scrap_info


# ==========================================
# Example Usage in Inches:
# ==========================================
plate_w_in, plate_h_in = 12.0, 8.0  # 12" x 8" Engraving Plate

# Label batch with semi-major (semi_a) and semi-minor (semi_b) axes in inches
labels_in = [
    {'id': 'Label_1_large_ellipse', 'semi_a': 1.75, 'semi_b': 0.875}, # 3.5" x 1.75" full size
    {'id': 'Label_2_large_ellipse', 'semi_a': 1.75, 'semi_b': 0.875},
    {'id': 'Label_3_circle',        'semi_a': 1.25, 'semi_b': 1.25},  # 2.5" diameter
    {'id': 'Label_4_small_ellipse', 'semi_a': 1.00, 'semi_b': 0.50},  # 2.0" x 1.0" full size
    {'id': 'Label_5_small_ellipse', 'semi_a': 1.00, 'semi_b': 0.50},
    {'id': 'Label_6_circle',        'semi_a': 0.75, 'semi_b': 0.75},  # 1.5" diameter
]

# Set resolution to 0.05" and tool clearance/kerf to 0.0625" (1/16")
packer = EllipsePacker(plate_width_in=plate_w_in, plate_height_in=plate_h_in, step_size_in=0.05)

# Mode A: PREFER_WIDTH (Maximizes clean vertical scrap on the right)
layout_w, scrap_w = packer.pack(labels_in, mode="PREFER_WIDTH", clearance=0.0625)
print("--- PREFER_WIDTH MODE ---")
print(f"Used Width:  {scrap_w['used_width_in']}\" / {plate_w_in}\"")
print(f"Scrap Strip: {scrap_w['salvaged_scrap_rect_in'][0]}\" W x {scrap_w['salvaged_scrap_rect_in'][1]}\" H\n")

# Mode B: PREFER_HEIGHT (Maximizes clean horizontal scrap on the top)
layout_h, scrap_h = packer.pack(labels_in, mode="PREFER_HEIGHT", clearance=0.0625)
print("--- PREFER_HEIGHT MODE ---")
print(f"Used Height: {scrap_h['used_height_in']}\" / {plate_h_in}\"")
print(f"Scrap Strip: {scrap_h['salvaged_scrap_rect_in'][0]}\" W x {scrap_h['salvaged_scrap_rect_in'][1]}\" H")