"""Keep R2 regressions; R4 uses explicit per-frame sustained-swing thresholds."""
from tests import revision2_checks as checks
ApiRevisionTests = checks.ApiRevisionTests
DeliveryRevisionTests = checks.DeliveryRevisionTests
RegimeRevisionTests = checks.RegimeRevisionTests


class StructureRevisionTests(checks.StructureRevisionTests):
    def test_width_caps_and_major_confirmation(self):
        from eth.levels import PROFILES
        for frame, seconds in [('30m', 1800), ('4h', 14400)]:
            x = checks.structure_levels(checks.bars(seconds, count=240), frame, 4000, .01)
            self.assertTrue(x['zones'])
            for z in x['zones']:
                self.assertLessEqual(z['high']-z['low'], x['max_zone_width']+1e-6)
                self.assertGreaterEqual(z['confirmation_right_bars'], 2)
                self.assertGreater(z['confirmed_at'], z['pivot_at'])
                if z['tier'] == 'external':
                    self.assertGreaterEqual(z['departure_atr'], PROFILES[frame]['multiplier'])
                    self.assertGreaterEqual(z['confirmation_right_bars'], PROFILES[frame]['min_right'])
