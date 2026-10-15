// mattromano build: the input-setup wizard POSTs each audio step's instrument
// to /api/settings, so walking Guitar -> Bass -> Drums used to leave the
// player on the Bass profile (every song then opened on Bass). The wizard now
// puts a profile back when it ends; this pins which one.
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { _profileAfterWizard } = require(path.join(__dirname, '..', '..', '..', '..', 'plugins', 'input_setup', 'screen.js'));

test('guitar -> bass -> drums keeps the Lead Guitar profile it started on', () => {
    assert.equal(_profileAfterWizard('guitar-lead', ['guitar', 'bass', 'drums']), 'guitar-lead');
    assert.equal(_profileAfterWizard('guitar-rhythm', ['guitar', 'bass']), 'guitar-rhythm');
});

test('a bass player who set up bass stays on bass', () => {
    assert.equal(_profileAfterWizard('bass', ['guitar', 'bass']), 'bass');
});

test('setting up only an instrument the start profile is not: that instrument wins', () => {
    assert.equal(_profileAfterWizard('guitar-lead', ['bass', 'drums']), 'bass');
    assert.equal(_profileAfterWizard('bass', ['drums', 'guitar']), 'guitar-lead');
    assert.equal(_profileAfterWizard(null, ['bass']), 'bass');
});

test('no guitar/bass step: nothing to put back', () => {
    assert.equal(_profileAfterWizard('guitar-lead', ['drums', 'keys']), null);
    assert.equal(_profileAfterWizard('guitar-lead', []), null);
});
