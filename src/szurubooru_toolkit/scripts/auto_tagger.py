from __future__ import annotations

import argparse
import os
import urllib
from pathlib import Path
from time import sleep

from loguru import logger
from tqdm import tqdm

from szurubooru_toolkit import SauceNao
from szurubooru_toolkit import config
from szurubooru_toolkit import szuru
from szurubooru_toolkit.utils import collect_sources
from szurubooru_toolkit.utils import sanitize_tags
from szurubooru_toolkit.utils import scrape_sankaku
from szurubooru_toolkit.utils import shrink_img
from szurubooru_toolkit.utils import statistics


def parse_args() -> tuple:
    """
    Parse the input args to the script auto_tagger.py and set the object attributes accordingly.
    """

    parser = argparse.ArgumentParser(
        description='This script will automagically tag your szurubooru posts based on your input query.',
    )

    parser.add_argument(
        '--sankaku_url',
        default=None,
        help='Fetch tags from specified Sankaku URL instead of searching SauceNAO.',
    )
    parser.add_argument(
        'query',
        help='Specify a single post id to tag or a szuru query. E.g. "date:today tag-count:0"',
    )

    parser.add_argument(
        '--add-tags',
        default=None,
        help='Specify tags, separated by a comma, which will be added to all posts matching your query',
    )

    parser.add_argument(
        '--remove-tags',
        default=None,
        help='Specify tags, separated by a comma, which will be removed from all posts matching your query',
    )

    args = parser.parse_args()

    sankaku_url = args.sankaku_url
    logger.debug(f'sankaku_url = {sankaku_url}')

    query = args.query
    logger.debug(f'query = {query}')

    if 'type:' in query:
        logger.critical('Search token "type" is not allowed in queries!')
        exit()

    if '\'' in query:
        logger.warning(
            'Your query contains single quotes (\'). '
            'Consider using double quotes (") if the script doesn\'t behave as intended.',
        )

    add_tags = args.add_tags
    logger.debug(f'add_tags = {add_tags}')
    remove_tags = args.remove_tags
    logger.debug(f'remove_tags = {remove_tags}')

    if add_tags:
        add_tags = add_tags.split(',')
    if remove_tags:
        remove_tags = remove_tags.split(',')

    return sankaku_url, query, add_tags, remove_tags


def parse_saucenao_results(sauce: SauceNao, post, tmp_media_path):
    limit_reached = False
    tags, source, rating, limit_short, limit_long = sauce.get_metadata(
        config.szurubooru['public'],
        post.content_url,
        str(tmp_media_path),
    )

    # Get previously set sources and add new sources
    source = collect_sources(*source.splitlines(), *post.source.splitlines())

    if not limit_long == 0:
        # Sleep 35 seconds after short limit has been reached
        if limit_short == 0:
            logger.warning('Short limit reached for SauceNAO, trying again in 35s...')
            sleep(35)
    else:
        limit_reached = True
        logger.info('Your daily SauceNAO limit has been reached. Consider upgrading your account.')

    if tags:
        statistics(tagged=1)

    if limit_reached and config.auto_tagger['deepbooru_enabled']:
        config.auto_tagger['saucenao_enabled'] = False
        logger.info('Continuing tagging with Deepbooru only...')

    return sanitize_tags(tags), source, rating, limit_reached


def download_media(tmp_media_path: str | None, content_url: str) -> str:
    if not tmp_media_path:
        filename = content_url.split('/')[-1]
        tmp_file = urllib.request.urlretrieve(content_url, Path(config.auto_tagger['tmp_path']) / filename)[0]
    else:
        tmp_file = tmp_media_path

    # Shrink files >2MB
    if os.path.getsize(tmp_file) > 2000000:
        shrink_img(Path(config.auto_tagger['tmp_path']), Path(tmp_file))

    logger.debug(f'Trying to get result from tmp_file: {tmp_file}')

    return tmp_file


def set_tags_from_relations(post) -> None:
    # Copy artist, character and series from relations.
    # Useful for FANBOX/Fantia sets where the main post is uploaded to a Booru.
    for relation in post.relations:
        result = szuru.api.getPost(relation['id'])

        for relation_tag in result.tags:
            if not relation_tag.category == 'default' or not relation_tag.category == 'meta':
                post.tags.append(relation_tag.primary_name)


@logger.catch
def main(post_id: str = None, tmp_media_path: str = None) -> None:  # noqa C901
    """Placeholder"""

    # If this script/function was called from the upload-media script,
    # change output and behaviour of this script
    from_upload_media = True if post_id else False

    if not from_upload_media:
        logger.info('Initializing script...')
    else:
        config.auto_tagger['hide_progress'] = True

    if not config.auto_tagger['saucenao_enabled'] and not config.auto_tagger['deepbooru_enabled']:
        logger.info('Nothing to do. Enable either SauceNAO or Deepbooru in your config.')
        exit()

    # If posts are being tagged directly from upload-media script
    if not from_upload_media:
        sankaku_url, query, add_tags, remove_tags = parse_args()
    else:
        sankaku_url = None
        query = post_id
        add_tags = None
        remove_tags = None

    if config.auto_tagger['saucenao_enabled']:
        sauce = SauceNao(config)

    if config.auto_tagger['deepbooru_enabled']:
        from szurubooru_toolkit import Deepbooru

        deepbooru = Deepbooru(config.auto_tagger['deepbooru_model'])

    if not from_upload_media:
        logger.info(f'Retrieving posts from {config.szurubooru["url"]} with query "{query}"...')

    posts = szuru.get_posts(query, from_upload_media)
    total_posts = next(posts)

    if not from_upload_media:
        logger.info(f'Found {total_posts} posts. Start tagging...')

    if sankaku_url:
        if query.isnumeric():
            post = next(posts)
            post.tags, post.safety = scrape_sankaku(sankaku_url)
            post.source = sankaku_url

            try:
                szuru.update_post(post)
                statistics(tagged=1)
            except Exception as e:
                statistics(untagged=1)
                logger.error(f'Could not tag post with Sankaku: {e}')
        else:
            logger.critical('Can only tag a single post if you specify --sankaku_url.')
    else:
        for index, post in enumerate(
            tqdm(
                posts,
                ncols=80,
                position=0,
                leave=False,
                disable=config.auto_tagger['hide_progress'],
                total=int(total_posts),
            ),
        ):
            tags = []

            if not config.szurubooru['public'] or config.auto_tagger['deepbooru_enabled']:
                tmp_file = download_media(tmp_media_path, post.content_url)
            else:
                tmp_file = None

            if config.auto_tagger['saucenao_enabled']:
                tags, post.source, post.safety, limit_reached = parse_saucenao_results(
                    sauce,
                    post,
                    tmp_file,
                )

                if add_tags:
                    post.tags = list(set().union(post.tags, tags, add_tags))  # Keep previous tags, add user tags
                else:
                    post.tags = list(set().union(post.tags, tags))  # Keep previous tags, add user tags
            else:
                limit_reached = False

            if (not tags and config.auto_tagger['deepbooru_enabled']) or config.auto_tagger['deepbooru_forced']:
                tags, post.safety = deepbooru.tag_image(tmp_file, config.auto_tagger['deepbooru_threshold'])

                if post.relations:
                    set_tags_from_relations(post)

                if add_tags:
                    post.tags = list(set().union(post.tags, tags, add_tags))  # Keep previous tags and add user tags
                else:
                    post.tags = list(set().union(post.tags, tags))  # Keep previous tags

                if 'DeepBooru' in post.source:
                    post.source = post.source.replace('DeepBooru\n', '')
                    post.source = post.source.replace('\nDeepBooru', '')

                if 'Deepbooru' not in post.source:
                    post.source = collect_sources(post.source, 'Deepbooru')

                if tags:
                    statistics(deepbooru=1)
                else:
                    statistics(untagged=1)
            elif not tags:
                statistics(untagged=1)

            # Remove temporary image
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

            if remove_tags:
                [post.tags.remove(tag) for tag in remove_tags if tag in post.tags]

            # If any tags were collected with SauceNAO or Deepbooru, tag the post
            if tags:
                [post.tags.remove(tag) for tag in post.tags if tag == 'deepbooru' or tag == 'tagme']
                szuru.update_post(post)

            if limit_reached and not config.auto_tagger['deepbooru_enabled']:
                statistics(untagged=int(total_posts) - index - 1)  # Index starts at 0
                break

    if not from_upload_media:
        total_tagged, total_deepbooru, total_untagged, total_skipped = statistics()

        logger.success('Script has finished tagging.')
        logger.success(f'Total:     {total_posts}')
        logger.success(f'Tagged:    {str(total_tagged)}')
        logger.success(f'Deepbooru: {str(total_deepbooru)}')
        logger.success(f'Untagged:  {str(total_untagged)}')
        logger.success(f'Skipped:   {str(total_skipped)}')


if __name__ == '__main__':
    main()
