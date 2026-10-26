"""Template structure/style safeguards; these do not replace browser layout tests."""
import re
import unittest
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
