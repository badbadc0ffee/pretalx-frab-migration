frab-migration
==============

.. image:: https://img.shields.io/pypi/v/pretalx-frab-migration.svg
   :target: https://pypi.org/project/pretalx-frab-migration/
   :alt: PyPI version

This is a plugin for `pretalx`_.
Import conferences from frab into pretalx.

This version needs the current pretalx development model (2026.3.0.dev0).
It uses managed speaker profiles with contact email and import origin.
Stable pretalx 2026.2.1 does not provide all required fields.

Installation
------------

Install the plugin with pip, in the same environment as your pretalx
installation::

    (env)$ python -m pip install pretalx-frab-migration

Afterwards, run ``migrate`` and ``rebuild`` and restart your pretalx services,
just like after any pretalx update (see `performing updates`_ in the
administrator documentation).

Global settings
---------------

Sign in as an instance administrator. Select an organiser. Select "frab Migration"
in the navigation menu. You can also open
``/orga/organiser/<organiser-slug>/p/pretalx_frab_migration/``.

The page shows the selected organiser. This organiser is the target for
imports. The link is not shown on global pages or event pages.

Enter the base URL of the frab system. Use HTTP or HTTPS. You can leave the field
empty until you configure the connection.

Select the database type and enter the access settings:

* SQLite: Enter the full database file path on the pretalx server. The plugin
  clears the saved host, port, user, and password when you save SQLite settings.
* MySQL or PostgreSQL: Enter the database name. Enter the host and user if the
  database does not use the local defaults. The host can also be a socket path.
  Enter a port, or leave it empty to use the default port (MySQL: 3306;
  PostgreSQL: 5432).

Enter a database password if necessary. The form does not show the saved
password. Leave the field empty to keep it. Enter a new password to replace it.
To remove all database settings, select "Not configured" and save the form.
You can then enter new settings without a password.

Save stores the settings without a connection test.

Connect and select a conference
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Below the settings, select "Connect". The plugin uses the values in the form.
It reads the conference list and saves the settings if the read succeeds.
An empty password field uses the saved password.

After a successful connection, select a conference from the dropdown. Each entry
shows the conference title and acronym. The page shows a message if the database
has no conferences or if the plugin cannot read the list.

The plugin opens the source database for read access. It closes the connection
after it reads the list. Connect again to refresh the list. Conference selection
does not start an import.

SQLite uses Python's database driver. MySQL uses PyMySQL. PostgreSQL uses Psycopg.
The plugin package installs the server database drivers. The database user must
have permission to read ``conferences``, ``call_for_participations``, ``tracks``,
``languages``, ``people``, ``events``, and ``event_people``.
Only fields from the December 2020 frab schema are used.

The plugin stores these settings for the full pretalx installation. Only instance
administrators can read or change them. The plugin does not need activation for
an event.

Migrate the event and speakers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Select a conference. Enter any missing required event values. Review the speakers,
then select "Migrate". The plugin returns to the migration dialog after success.
It creates a public event under the selected organiser. It uses pretalx's native
``create_event`` and ``post_create_event`` functions.

The import includes event dates, contact email, colour, feedback, introductory
text, the HTML export URL, CfP dates and text, and conference languages.
If the event email is empty, the CfP email is used before asking for a value.
Track and session type imports are optional when the source has several choices.
Lectures map to the native Talk type. CfP text comes only from frab.
The CfP uses the hard deadline, with the end date as fallback.

Rails timezone names are converted to IANA names. Source timestamps without an
offset use UTC. The conference timezone determines the event dates. A child
conference uses its parent's dates and timezone. The HTML export URL is copied
without conditions. The plugin does not import talks, rooms, schedules, or files.

An unrelated event with the same short name stops event creation. Select a new
short name where the form permits it. A mapped earlier import updates its speakers
without creating another event. "Previously imported event" can associate an
older import in the current organiser with its source conference. This action
updates speakers only.

Speaker matching and review
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The import selects distinct speakers and moderators of the chosen conference.
A matching pretalx user must have both the same email and the same name. Email
comparison ignores case. Name comparison removes surrounding spaces and keeps
case, accents, and punctuation significant. The source name is the public name,
with first and last name as fallback. Global user data stays unchanged.

Without a matching user, the plugin creates a managed speaker. It creates no
login accounts and sends no invitations or other mail. It imports the original
biography and private notes. Missing names and invalid emails need correction.
An invalid email can be cleared explicitly.

Shared email addresses and conflicting profile fields need review. Several frab
IDs can be aliases for one profile. Select a primary source for its fields and URL.
Existing profile edits are preserved. If both systems changed a field, select
which value to keep. "Update preview" applies review choices before migration.
A change to the source or target after the preview requires a fresh review.

The importer uses native profile merging when a managed speaker must join an
existing account profile. It moves talk links and answers and asks for field
choices. A profile deleted or merged elsewhere needs reference repair.

Private frab source links
~~~~~~~~~~~~~~~~~~~~~~~~~

A native URL field named "frab source URL" links each speaker to::

    <frab base URL>/<original conference acronym>/people/<person ID>

The URL preserves any installation path in the configured base URL. It can
require a frab login. The field is hidden from public and reviewer-only views.
Speakers can see their own value. Standard forms cannot edit it. If the base URL
is empty, the import omits these links and reports this through Django messages.

Import references
~~~~~~~~~~~~~~~~~

The plugin adds no database models or tables. Native event settings store source
IDs, profile codes, alias choices, and hashes of previous field values. The native
custom field stores the source URL. Existing account/profile constraints and a
lock on the native global source setting protect concurrent imports.

Keep "Use a different frab database" clear when changing passwords, hosts, or the
base URL. Select it only when changing to another source database. Settings changes
require a new connection. Connections and previews expire after one hour.

Event creation and all imported data use one transaction. A failed import leaves
no partial batch. Source database content stays unchanged. Deleting a pretalx event
also deletes its import references, because these are native event settings.

Development setup
-----------------

1. Make sure that you have a working `pretalx development setup`_.

2. Clone this repository, eg to ``local/pretalx-frab-migration``.

3. Activate the virtual environment you use for pretalx development.

4. Run ``pip install -e .`` within this directory to register this application with pretalx's plugin registry.

5. Restart your local pretalx server. Open the global settings page as an instance
   administrator.

Development commands
~~~~~~~~~~~~~~~~~~~~

This plugin uses `just`_ as a task runner and `uv`_ for dependency management.
Run ``just`` with no arguments to list every available command. The most useful ones
are:

``just fmt``
    Auto-format and lint the code.

``just test``
    Run the full test suite with pytest.

Installing pretalx
~~~~~~~~~~~~~~~~~~

The tests need pretalx installed in the environment. ``just test`` handles this for
you: if pretalx cannot be imported, it installs the latest version from git before
running the test suite.

If you already have a development version of pretalx around (for example if you want
to test your changes against a specific commit or branch of pretalx), you can also
install pretalx up front yourself:

``just install-pretalx-local /path/to/pretalx``
    Install pretalx from a local checkout as an editable install.

``just install-pretalx``
    Install the latest pretalx from git (runs before tests if no pretalx is installed).


License
-------

Copyright 2026 Florian Mösch

Released under the terms of the Apache License 2.0


.. _pretalx: https://github.com/pretalx/pretalx
.. _pretalx development setup: https://docs.pretalx.org/en/latest/developer/setup.html
.. _just: https://just.systems/
.. _uv: https://docs.astral.sh/uv/
.. _performing updates: https://docs.pretalx.org/administrator/maintenance/#performing-updates
