-- Paprika local mirror (Paprika cloud = master)
-- UIDs are text: Paprika sometimes uses non-RFC UUID strings.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sync_state (
    entity          text PRIMARY KEY,
    remote_counter  bigint NOT NULL DEFAULT 0,
    last_sync_at    timestamptz,
    notes           text
);

CREATE TABLE IF NOT EXISTS categories (
    uid             text PRIMARY KEY,
    name            text NOT NULL DEFAULT '',
    parent_uid      text,
    order_flag      integer NOT NULL DEFAULT 0,
    deleted         boolean NOT NULL DEFAULT false,
    raw_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS categories_parent_idx ON categories (parent_uid);
CREATE INDEX IF NOT EXISTS categories_name_trgm ON categories USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS recipes (
    uid             text PRIMARY KEY,
    hash            text NOT NULL,
    name            text NOT NULL DEFAULT '',
    ingredients     text NOT NULL DEFAULT '',
    directions      text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    notes           text NOT NULL DEFAULT '',
    nutritional_info text NOT NULL DEFAULT '',
    servings        text NOT NULL DEFAULT '',
    servings_min    integer,
    servings_max    integer,
    difficulty      text NOT NULL DEFAULT '',
    prep_time       text NOT NULL DEFAULT '',
    prep_minutes    integer,
    cook_time       text NOT NULL DEFAULT '',
    cook_minutes    integer,
    total_time      text NOT NULL DEFAULT '',
    total_minutes   integer,
    source          text NOT NULL DEFAULT '',
    source_url      text NOT NULL DEFAULT '',
    image_url       text,
    photo           text,
    photo_hash      text,
    photo_large     text,
    photo_url       text,
    scale           text,
    rating          integer NOT NULL DEFAULT 0,
    in_trash        boolean NOT NULL DEFAULT false,
    is_pinned       boolean NOT NULL DEFAULT false,
    on_favorites    boolean NOT NULL DEFAULT false,
    on_grocery_list boolean NOT NULL DEFAULT false,
    cookbook_uid    text,
    -- Paprika's "created" field; often rewritten on edit — NOT a reliable updated_at
    paprika_created text,
    present_in_remote boolean NOT NULL DEFAULT true,
    raw_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipes_hash_idx ON recipes (hash);
CREATE INDEX IF NOT EXISTS recipes_in_trash_idx ON recipes (in_trash);
CREATE INDEX IF NOT EXISTS recipes_present_idx ON recipes (present_in_remote);
CREATE INDEX IF NOT EXISTS recipes_name_trgm ON recipes USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS recipes_ingredients_trgm ON recipes USING gin (ingredients gin_trgm_ops);
CREATE INDEX IF NOT EXISTS recipes_fts_idx ON recipes USING gin (
    to_tsvector(
        'english',
        coalesce(name, '') || ' ' ||
        coalesce(ingredients, '') || ' ' ||
        coalesce(directions, '') || ' ' ||
        coalesce(description, '') || ' ' ||
        coalesce(notes, '')
    )
);

CREATE TABLE IF NOT EXISTS recipe_categories (
    recipe_uid      text NOT NULL REFERENCES recipes (uid) ON DELETE CASCADE,
    category_uid    text NOT NULL REFERENCES categories (uid) ON DELETE CASCADE,
    PRIMARY KEY (recipe_uid, category_uid)
);

CREATE INDEX IF NOT EXISTS recipe_categories_category_idx
    ON recipe_categories (category_uid);

CREATE TABLE IF NOT EXISTS recipe_index (
    uid             text PRIMARY KEY,
    hash            text NOT NULL,
    synced_at       timestamptz NOT NULL DEFAULT now()
);
