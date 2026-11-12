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
            pill = self.responsive_properties('.panel-stats > .pill:not(.panel-stats-channel)', width)
            self.assertEqual(('normal', 'visible', 'clip'),
                             (pill['white-space'], pill['overflow'], pill['text-overflow']))

    def test_desktop_channel_retains_original_clipping(self):
        for width in (901, 1024, 1280):
            pill = self.responsive_properties('.panel-stats .pill', width)
            self.assertEqual(('nowrap', 'hidden', 'ellipsis'),
                             (pill['white-space'], pill['overflow'], pill['text-overflow']))
        for width in (320, 390, 900):
            self.assertEqual({}, self.responsive_properties('.panel-stats > .pill:not(.panel-stats-channel)', width))

    def test_mobile_metadata_retains_horizontal_layout_and_original_overflow(self):
        for width in (320, 390, 900):
            self.assertEqual('flex', self.responsive_properties('.panel-stats', width)['display'])
            self.assertNotIn('grid-column', self.responsive_properties('#stat', width))
            pill = self.responsive_properties('.panel-stats .pill', width)
            self.assertEqual(('nowrap', 'hidden', 'ellipsis'),
                             (pill['white-space'], pill['overflow'], pill['text-overflow']))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for overlay behavior tests')
    def test_vod_link_and_embedded_image_javascript(self):
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
        self.assertEqual('var(--preview-ratio,16/9)', re.sub(r'\s+', '', self.properties('.vod-card-media')['aspect-ratio']))
        image = self.properties('#vodPreviewImg')
        self.assertEqual('block', image.get('display'))
        self.assertEqual('100%', image.get('width'))
        self.assertEqual('100%', image.get('height'))
        self.assertEqual('contain', image.get('object-fit'))

    def test_mobile_compact_panel_uses_a_clipped_measured_section(self):
        self.assertEqual('hidden',self.properties('.panel-details')['overflow'])
        self.assertEqual('0',self.properties('.panel-details')['height'])
        self.assertEqual('flex',self.responsive_properties('.panel-details',1440)['display'])
        self.assertEqual('auto',self.responsive_properties('.panel-details',1440)['overflow'])
        self.assertEqual('none',self.responsive_properties('.desktop-preview',390)['display'])

    def test_mobile_live_warning_and_expanded_metadata_are_scoped(self):
        for width in (320,390,900):
            self.assertEqual('hidden',self.responsive_properties('.panel-details',width)['overflow'])
            self.assertEqual('flex',self.responsive_properties('header.is-expanded .panel-stats',width)['display'])
            channel=self.responsive_properties('header.is-expanded #mobileChannel',width)
            self.assertEqual(('1 / -1','normal','anywhere'),(channel['grid-column'],channel['white-space'],channel['overflow-wrap']))
            selectors=['header .panel-stats','header .live-capture-warning','header .controls']
            self.assertEqual(['1','2','3'],[self.responsive_properties(s,width)['order'] for s in selectors])
        for selector in ['header.is-compact .live-capture-warning','header.is-expanded .panel-stats','header.is-expanded #mobileChannel']:
            self.assertEqual({},self.responsive_properties(selector,1440))

    def test_scrubber_and_toggle_row_are_mobile_only(self):
        for width in (320,390,900):
            self.assertEqual('flex',self.responsive_properties('.fast-scrubber',width)['display'])
            self.assertEqual('var(--control-h)',self.responsive_properties('.fast-scrubber',width)['min-height'])
            self.assertEqual('0',self.responsive_properties('header.is-compact',width)['gap'])
            tab=self.responsive_properties('.panel-toggle',width)
            self.assertEqual('flex',tab['display'])
            self.assertEqual(('var(--control-h)','var(--control-h)'),(tab['width'],tab['height']))
            self.assertNotIn('position',tab)
            self.assertEqual({},self.responsive_properties('header::after',width))
        for width in (901,1440):
            self.assertEqual('none',self.responsive_properties('.mobile-control-row',width)['display'])

    def test_scrubber_label_uses_one_glyph_mask_with_two_contrasting_colors(self):
        label=self.properties('.scrub-label')
        self.assertEqual('center',label['justify-content'])
        self.assertEqual('nowrap',label['white-space'])
        self.assertEqual('transparent',label['color'])
        self.assertEqual('text',label['background-clip'])
        self.assertIn('--scrub-label-edge',label['background'])
        self.assertEqual(['#20212b','#e1e3ed'],re.findall(r'#[0-9a-f]{6}',label['background']))
        self.assertEqual({},self.properties('.scrub-label-on-fill'))
        self.assertEqual('none',self.properties('.scrub-thumb')['pointer-events'])
        def luminance(color):
            rgb=[int(color[i:i+2],16)/255 for i in (1,3,5)]
            linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
            return sum(v*w for v,w in zip(linear,(.2126,.7152,.0722)))
        for text,bg in [('#e1e3ed',self.properties('.scrub-track')['background']),
                        ('#20212b',self.properties('.scrub-fill')['background'])]:
            a,b=sorted((luminance(text),luminance(bg)))
            self.assertGreaterEqual((b+.05)/(a+.05),4.5)

    def test_mobile_controls_use_explicit_equal_columns_and_shared_height(self):
        for width in (320,390,900):
            for selector in ('.controls','.meta-buttons'):
                props=self.responsive_properties(selector,width)
                self.assertEqual('repeat(2,minmax(0,1fr))',props['grid-template-columns'])
                self.assertEqual('var(--control-gap)',props['gap'])
            for selector,col in [('.controls #q','1'),('.controls #u','2')]:
                props=self.responsive_properties(selector,width)
                self.assertEqual(col,props['grid-column'])
                self.assertEqual('auto',props['grid-row'])
            self.assertEqual('auto',self.responsive_properties('header.is-expanded #stat',width)['margin-left'])
            self.assertEqual('right',self.responsive_properties('header.is-expanded #stat',width)['text-align'])

    def test_mobile_metadata_keeps_date_and_timezone_in_one_group(self):
        for width in (320,390):
            date=self.responsive_properties('#streamDate',width)
            self.assertEqual(('inline-flex','6px'),(date['display'],date['gap']))
            self.assertEqual('nowrap',self.responsive_properties('.panel-stats .meta-value',width)['white-space'])
            self.assertEqual('wrap',self.responsive_properties('header.is-expanded .panel-stats',width)['flex-wrap'])
        self.assertEqual({},self.responsive_properties('.panel-stats .meta-value',1440))

    def test_mobile_track_has_one_clip_and_inverted_palette(self):
        self.assertEqual('#30313e',self.properties('.scrub-track')['background'])
        self.assertEqual('#a0a3b7',self.properties('.scrub-fill')['background'])
        self.assertEqual('hidden',self.properties('.scrub-track')['overflow'])
        self.assertNotIn('border',self.properties('.scrub-track'))
        self.assertEqual('18px',self.properties('.fast-scrubber')['--scrub-thumb-width'])
        self.assertEqual('#a0a3b7',self.properties('.scrub-thumb')['background'])
        self.assertEqual('border-box',self.properties('.scrub-thumb')['background-origin'])
        self.assertIn('--scrub-ratio',self.properties('.fast-scrubber')['--scrub-center'])
        self.assertEqual('var(--scrub-inset)',self.properties('.scrub-track')['left'])
        self.assertEqual('var(--scrub-inset)',self.properties('.scrub-track')['right'])

    def test_mobile_list_background_excludes_top_reserve_but_keeps_bottom_padding(self):
        for width in (320,390):
            paint=self.responsive_properties('#list',width)
            self.assertEqual('transparent',paint['background-color'])
            self.assertEqual('64px',paint['padding-top'])
            self.assertEqual('content-box',paint['background-origin'])
            self.assertEqual('100% calc(100% + 40px)',paint['background-size'])
            self.assertEqual('40px',paint['padding-bottom'])
        self.assertNotIn('background-origin',self.responsive_properties('#list',1440))

    def test_mobile_focus_is_visible_without_glow_or_pressed_geometry_changes(self):
        for selector in ['header .vod-card:active','header .meta-buttons button:active']:
            props=self.responsive_properties(selector,390)
            self.assertEqual(('none','none'),(props['transform'],props['box-shadow']))
        for selector in ['header .vod-card:focus-visible','header .controls input:focus-visible',
                         'header .meta-buttons button:focus-visible','.panel-toggle:focus-visible']:
            focus=self.responsive_properties(selector,390)
            self.assertEqual(('2px solid #b3adff','0','transparent'),(focus['outline'],focus['outline-offset'],focus['border-color']))
        self.assertEqual('visible',self.responsive_properties('header.is-expanded:not(.is-panel-animating) .panel-details',390)['overflow'])


if __name__ == '__main__':
    unittest.main()
