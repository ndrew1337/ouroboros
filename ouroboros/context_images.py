"""Physical image floors and exact-source placeholders; no content judgment."""
from __future__ import annotations

import copy


def image_placeholder(block):
    caption = str(block.get('_caption') or '').strip()
    source = str(block.get('_source_path') or '').strip()
    return {'type': 'text', 'text': ('[image evicted' + (f': {caption}' if caption else '')
            + (f'; re-view: view_image path={source}' if source else '') + ']')}


def image_positions(messages):
    return [(mi, bi) for mi, row in enumerate(messages) if isinstance(row.get('content'), list)
            for bi, block in enumerate(row['content'])
            if isinstance(block, dict) and block.get('type') in {'image_url', 'image'}]


def evict_stale_image_blocks(messages, *, incoming=0, owner_mode='max'):
    """Nano chosen by the owner batches >4 to 2; Max/Low keep their >10 to 5 floor."""
    from ouroboros.context_budget import (MAX_LIVE_IMAGE_BLOCKS, LIVE_IMAGE_BLOCKS_HIGH_WATER,
                                          NANO_LIVE_IMAGE_BLOCKS, NANO_LIVE_IMAGE_BLOCKS_HIGH_WATER)
    keep, high = (NANO_LIVE_IMAGE_BLOCKS, NANO_LIVE_IMAGE_BLOCKS_HIGH_WATER) if owner_mode == 'nano' else (MAX_LIVE_IMAGE_BLOCKS, LIVE_IMAGE_BLOCKS_HIGH_WATER)
    positions = image_positions(messages)
    total = len(positions) + max(0, int(incoming))
    if total <= high:
        return False
    for mi, bi in positions[:total - keep]:
        messages[mi]['content'][bi] = image_placeholder(messages[mi]['content'][bi])
    return True


def fresh_call_images(messages, *, owner_mode='max'):
    """A newer stamped delivery replaces only that source's earlier pixels in this send copy.

    No stamp or no exact reader means no supersession. Canonical bytes stay intact.
    Neither filenames nor pixel similarity define a capture source.
    """
    rows, seen = copy.deepcopy(messages), set()
    evict_stale_image_blocks(rows, owner_mode=owner_mode)
    for mi, bi in reversed(image_positions(rows)):
        block = rows[mi]['content'][bi]
        source = block.get('_capture_source')
        if not source:
            continue
        if source in seen and block.get('_source_path'):
            rows[mi]['content'][bi] = image_placeholder(block)
        seen.add(source)
    return rows
