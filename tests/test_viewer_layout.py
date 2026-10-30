"""Template structure/style safeguards; these do not replace browser layout tests."""
import re
import unittest
import shutil
import subprocess
from pathlib import Path
from html.parser import HTMLParser

from src.html_renderer import render_viewer_html


class PreviewParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.parents = {}
        self.styles = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') in ('vodCard', 'vodPreviewImg'):
            self.parents[attrs['id']] = tuple(self.stack)
        if tag not in ('meta', 'img', 'input', 'link', 'br', 'hr'):
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][0] == 'style':
            self.styles.append(data)


class ViewerPreviewLayoutTests(unittest.TestCase):
    def setUp(self):
        self.page = PreviewParser(render_viewer_html([], 'Long title '*30, 'channel', 'vod'))
        css = re.sub(r'/\*.*?\*/', '', ''.join(self.page.styles), flags=re.S)
        # Read leaf rules (including media-query contents) without depending on
        # whitespace or declaration order. This is not a CSS cascade engine.
        self.rules = []
        for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
            declarations = {}
            for declaration in match[2].split(';'):
                name, separator, value = declaration.partition(':')
                if separator:
                    declarations[name.strip()] = value.strip()
            self.rules.append(([selector.strip() for selector in match[1].split(',')], declarations))

    def properties(self, selector):
        properties = {}
        for selectors, declarations in self.rules:
            if selector in selectors:
                properties.update(declarations)
        return properties

    def responsive_properties(self, selector, width):
        # Resolve exact-selector overrides inside the template's width queries.
        # Browser QA separately checks actual layout and computed styles.
        css = re.sub(r'/\*.*?\*/', '', ''.join(self.page.styles), flags=re.S)
        properties = {}

        def visit(source):
            cursor = 0
            while cursor < len(source):
                opening = source.find('{', cursor)
                if opening < 0:
                    break
                prelude = source[cursor:opening].strip()
                depth, end = 1, opening + 1
                while depth:
                    depth += (source[end] == '{') - (source[end] == '}')
                    end += 1
                body = source[opening + 1:end - 1]
                if prelude.startswith('@media'):
                    bounds = re.findall(r'(min|max)-width:\s*(\d+)px', prelude)
                    if all(width >= int(n) if kind == 'min' else width <= int(n)
                           for kind, n in bounds):
                        visit(body)
                elif selector in [s.strip() for s in prelude.split(',')]:
                    for declaration in body.split(';'):
                        name, sep, value = declaration.partition(':')
                        if sep:
                            properties[name.strip()] = value.strip()
                cursor = end
        visit(css)
        return properties

    def test_desktop_metadata_has_full_width_left_aligned_rows(self):
        for width in (901, 1024, 1280):
            self.assertEqual('minmax(0, 1fr)', self.responsive_properties('.panel-stats', width)['grid-template-columns'])
            stat = self.responsive_properties('#stat', width)
            self.assertEqual('1 / -1', stat['grid-column'])
            self.assertEqual('start', stat['justify-self'])
            pill = self.responsive_properties('.panel-stats .pill', width)
            self.assertEqual(('normal', 'visible', 'clip'),
                             (pill['white-space'], pill['overflow'], pill['text-overflow']))

    def test_mobile_metadata_retains_horizontal_layout_and_original_overflow(self):
        for width in (320, 390, 900):
            self.assertEqual('flex', self.responsive_properties('.panel-stats', width)['display'])
            self.assertNotIn('grid-column', self.responsive_properties('#stat', width))
            pill = self.responsive_properties('.panel-stats .pill', width)
            self.assertEqual(('nowrap', 'hidden', 'ellipsis'),
                             (pill['white-space'], pill['overflow'], pill['text-overflow']))

    def test_vod_action_width_override_is_scoped_away_from_png_snapshots(self):
        self.assertEqual('var(--controls-w)', self.properties('.snapshot-actions')['max-width'])
        action = self.properties('.vod-preview-panel .snapshot-actions')
        self.assertEqual(('0', '100%', 'none'), (action['width'], action['min-width'], action['max-width']))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for overlay behavior tests')
    def test_vod_overlay_javascript(self):
        result = subprocess.run([shutil.which('node'), '--test', str(Path(__file__).with_name('test_vod_preview.js'))],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_grid_does_not_clip_preview_effects_or_stretch_card_to_title(self):
        panel = self.properties('.panel-top')
        self.assertEqual('visible', panel.get('overflow'))
        for axis in ('overflow-x', 'overflow-y'):
            self.assertEqual('visible', panel.get(axis, panel['overflow']))
        self.assertEqual('start', self.properties('.vod-card').get('align-self'))

    def test_image_remains_inside_the_rounded_interactive_card(self):
        parents = self.page.parents['vodPreviewImg']
        self.assertEqual('vodCard', parents[-2][1].get('id'))
        self.assertIn('vod-card-media', parents[-1][1].get('class', '').split())
        self.assertIn('panel-top', self.page.parents['vodCard'][-1][1].get('class', '').split())
        self.assertEqual('hidden', self.properties('.vod-card').get('overflow'))
        self.assertEqual('16/9', re.sub(r'\s+', '', self.properties('.vod-card-media')['aspect-ratio']))
        image = self.properties('.vod-card-media img')
        self.assertEqual('block', image.get('display'))
        self.assertEqual('100%', image.get('width'))
        self.assertEqual('100%', image.get('height'))
        self.assertEqual('cover', image.get('object-fit'))

    def test_mobile_compact_panel_still_hides_and_disables_preview(self):
        compact = self.properties('header.is-compact .panel-top')
        expanded = self.properties('header.is-expanded .panel-top')
        self.assertEqual('0', compact.get('opacity'))
        self.assertEqual('none', compact.get('pointer-events'))
        self.assertEqual('1', expanded.get('opacity'))
        self.assertEqual('none', expanded.get('transform'))


if __name__ == '__main__':
    unittest.main()
