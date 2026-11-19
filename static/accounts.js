KEV.accounts = {
  descriptions: {
    admin: 'Manage access, settings, content, reports and backups.',
    organizer: 'Run the programme, event communications, quiz and audience display.',
    operator: 'Run published engagement activities and control their hall display.',
    scoring: 'Run judging clocks, review sheets, manage feedback and publish results.',
    registration: 'Check arrivals, manage teams and help participants sign in.',
    judge: 'Work at an assigned judging station and on assigned Final panels.',
    participant: 'Open one roster member’s team workspace and released results.',
    guest: 'View the public programme and audience updates.'
  },
  linkPhrase: 'LINK AND RETIRE SOURCE PASSWORD',
  busy: false,
  get dirty() {return !!document.querySelector('#kevenos-account-editor [data-account-dirty="1"]');},
  assignment(grant) {
    if (grant.role === 'judge') return grant.station ? 'Station ' + grant.station : 'Station needed';
    if (grant.role === 'participant') return grant.pair && grant.member ? 'Team ' + grant.pair + ' · Person ' + grant.member : 'Roster assignment needed';
    return '';
  },
  grantLabel(grant) {
    return [KEV.profile.label(grant.role), this.assignment(grant)].filter(Boolean).join(' · ');
  },
  status(account) {
    if (!account.credentials.some(credential=>credential.active)) return 'No active password';
    return account.grants.length ? 'Active' : 'Access setup needed';
  },
  mount(container, {accounts, roles, roster, stations, mode, actorId, save, reload}) {
    const esc = KEV.esc;
    const rehearsal = mode === 'rehearsal';
    const dates = new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short'});
    accounts = accounts.filter(account=>!account.grants.some(g=>g.role==='guest'));
    container.innerHTML = '<section class="card"><h2>People and access</h2>' +
      '<p>Assign dashboards to existing people. Participants come from the roster. Admin creates Staff accounts; roles determine their dashboards. Use Participant passwords or Staff passwords to issue and share access.</p>' +
      (rehearsal ? '<p class="mut">Judge and Participant assignments below apply to Rehearsal. Shared staff roles can be changed here with confirmation; account linking requires Live.</p>' : '') +
      '<label class="field">Account type<select id="kevenos-account-type"><option value="">All people</option><option value="staff">Staff</option><option value="participant">Participant</option></select></label>' +
      (accounts.length ? '<div class="table-scroll"><table><thead><tr><th scope="col">Name</th><th scope="col">Role and assignment</th><th scope="col">Sign-in status</th><th scope="col">Last sign-in</th><th scope="col">Access</th></tr></thead><tbody>' +
      accounts.map((account,index)=>{
        const last = Math.max(0,...account.credentials.map(c=>Number(c.last_used) || 0));
        return '<tr data-account-type="' + esc(account.account_type || (account.grants.some(g=>g.role!=='participant')?'staff':'participant')) + '"><th scope="row">' + esc(account.label) + '</th><td>' +
          (account.grants.map(grant=>'<div>' + esc(this.grantLabel(grant)) + '</div>').join('') || 'No dashboards') +
          '</td><td>' + this.status(account) + '</td><td>' + (last ? esc(dates.format(last * 1000)) : '—') +
          '</td><td><button class="btn ghost sm" type="button" data-account-index="' + index + '" data-account-id="' + esc(account.id) + '" aria-label="Edit access for ' + esc(account.label) + '">Edit access</button></td></tr>';
      }).join('') + '</tbody></table></div>' : '<p>No person accounts yet.</p>') + '</section><section id="kevenos-account-editor" class="card kevenos-account-editor" aria-labelledby="kevenos-account-name" hidden></section>';
    container.querySelector('#kevenos-account-type').onchange=event=>{container.querySelectorAll('[data-account-type]').forEach(row=>row.hidden=!!event.target.value && row.dataset.accountType!==event.target.value);};
    const editor = container.querySelector('#kevenos-account-editor');
    const sources = accounts.filter(account=>account.id!==actorId && account.credentials.some(c=>c.active) && !account.grants.some(grant=>grant.role==='guest'));
    container.querySelectorAll('[data-account-index]').forEach(button=>button.onclick=()=>{
      if (this.busy || (this.dirty && !confirm('Discard unsaved access changes?'))) return;
      editor.hidden=false;
      this.edit(editor,accounts[Number(button.dataset.accountIndex)], {roles, roster, stations, rehearsal, actorId, sources, save, reload});
      editor.querySelector('h2').focus();
    });
  },
  edit(editor, account, {roles, roster, stations, rehearsal, actorId, sources, save, reload}) {
    const esc = KEV.esc;
    const assigned = new Map(account.grants.map(grant=>[grant.role, grant]));
    const scopes = {
      judge:{label:'Judging station', options:stations.map(station=>[station, 'Station '+station]), value:assigned.get('judge')?.station},
      participant:{
        label:'Roster member and team',
        options:roster.flatMap(row=>KEV.memberList(row).filter(member=>member.name.trim()).map(member=>[
          row.pair + ':' + member.id, member.name + ' · Team ' + row.pair
        ])),
        value:assigned.has('participant') ? assigned.get('participant').pair + ':' + assigned.get('participant').member : ''
      }
    };
    const reason = '<label class="field">Reason<textarea name="reason" rows="2" required minlength="3" maxlength="300"></textarea></label>';
    const title = '<h2 id="kevenos-account-name" tabindex="-1">' + esc(account.label) + '</h2>';
    if (assigned.has('guest')) {
      editor.innerHTML = title + '<p>This shared password opens Event information. Manage it in Passwords.</p>';
      return;
    }
    const roleControls = roles.filter(role=>role.id!=='guest').map(({id,label})=>{
      const locked = (id==='admin' && account.id===actorId && assigned.has(id));
      const scope = scopes[id];
      const optionHtml = scope?.options.map(([value,name])=>'<option value="' + esc(value) + '"' +
        (value===scope.value?' selected':'') + '>' + esc(name) + '</option>').join('');
      return '<div class="kevenos-account-role" data-role="' + esc(id) + '"><label><input type="checkbox" name="role" value="' +
        esc(id) + '"' + (assigned.has(id)?' checked':'') + (locked?' disabled':'') + '> <span><strong>' + esc(KEV.profile.label(id)) +
        '</strong> <span class="small mut">' + esc(this.descriptions[id]) + '</span></span></label>' +
        (scope ? '<label class="field" data-assignment hidden>' + scope.label + '<select name="' + esc(id) +
        '"><option value="">Choose ' + (id==='judge' ? 'a station' : 'a roster member') + '</option>' + optionHtml + '</select></label>' : '') +
        '<span class="small mut">' + esc(label) + (locked && id==='admin' && account.id===actorId ? ' · Current account' : '') + '</span></div>';
    }).join('');
    const linkSources = sources.filter(source=>source.id!==account.id);
    const linkForm = !rehearsal && account.credentials.some(c=>c.active) && linkSources.length ?
      '<details class="kevenos-account-link"><summary>Link an existing account</summary>' +
      '<p>Verify that both accounts belong to the same person. The selected source password is retired and its roles move to ' + esc(account.label) +
      '. Keep using the active password for ' + esc(account.label) + '. Both accounts’ sessions are signed out.</p>' +
      '<form id="kevenos-account-link"><label class="field">Source account<select name="source" required>' +
      '<option value="">Choose an account</option>' + linkSources.map((source,index)=>'<option value="' + index + '">' +
      esc(source.label) + ' · ' + esc(source.grants.map(grant=>this.grantLabel(grant)).join(', ') || 'No dashboards') +
      '</option>').join('') + '</select></label><p class="small mut" data-link-preview></p>' + reason +
      '<label class="field">Type <strong>' + this.linkPhrase + '</strong> to confirm<input name="confirm" autocomplete="off" autocapitalize="off" spellcheck="false" required pattern="' + this.linkPhrase + '"></label>' +
      '<button class="btn bad" type="submit">Link and retire password</button><p role="alert" data-error></p></form></details>' : '';
    editor.innerHTML = title + '<form id="kevenos-account-grants"><fieldset class="kevenos-account-roles"><legend>Roles</legend>' + roleControls +
      '</fieldset><p class="small" data-role-preview></p>' +
      (rehearsal ? '<div data-shared-confirm hidden><p>Shared staff roles apply in <strong>Live and Rehearsal</strong>. Judge and Participant assignments apply only to Rehearsal. The event will stay in Rehearsal.</p><label><input type="checkbox" name="confirm_shared_roles"> I confirm these shared staff-role changes for this person.</label></div>' : '') + reason +
      '<p class="small mut">Saving changes signs this person out. Their password stays the same.</p><button class="btn" type="submit">Save roles</button>' +
      '<p role="alert" data-error></p></form>' + linkForm;
    const grantsForm = editor.querySelector('#kevenos-account-grants');
    const link = editor.querySelector('#kevenos-account-link');
    const roleBlocks = [...grantsForm.querySelectorAll('[data-role]')];
    const roleLabels = new Map(roles.map(role=>[role.id,role.label]));
    const roleValues = () => roleBlocks.flatMap(block=>{
      const role = block.dataset.role;
      if (!block.querySelector('[name="role"]').checked) return [];
      const value = block.querySelector('select')?.value;
      if (role==='judge') return [{role,station:Number(value)}];
      if (role==='participant') {
        const [pair, member] = value.split(':').map(Number);
        return [{role,pair,member}];
      }
      return [{role}];
    });
    const sharedChanged = () => rehearsal && roleBlocks.some(block=>!scopes[block.dataset.role] &&
      block.querySelector('[name="role"]').checked !== assigned.has(block.dataset.role));
    const update = event => {
      const confirmation=grantsForm.querySelector('[data-shared-confirm]');
      if (confirmation) {
        const checkbox=grantsForm.elements.namedItem('confirm_shared_roles');
        confirmation.hidden=!sharedChanged();checkbox.required=sharedChanged();
        if (event?.target?.name==='role' || !sharedChanged()) checkbox.checked=false;
      }
      for (const block of roleBlocks) {
        const assignment = block.querySelector('[data-assignment]');
        if (!assignment) continue;
        const selected = block.querySelector('[name="role"]').checked;
        assignment.hidden = !selected;
        assignment.querySelector('select').disabled = !selected;
        assignment.querySelector('select').required = selected;
      }
      const grants = roleValues();
      grantsForm.querySelector('[data-role-preview]').textContent = grants.length ?
        'Dashboard access after saving: ' + grants.map(grant=>[roleLabels.get(grant.role),this.assignment(grant)].filter(Boolean).join(' · ')).join(', ') :
        'This person will have no dashboards.';
    };
    const submit = (form, values) => async event => {
      event.preventDefault();
      if (this.busy) return;
      const error = form.querySelector('[data-error]');
      const button = form.querySelector('[type="submit"]');
      const reason = form.elements.namedItem('reason').value.trim();
      if (reason.length < 3) {error.textContent='Enter a short reason for this access change.';return;}
      if (form===grantsForm && sharedChanged() && !form.elements.namedItem('confirm_shared_roles').checked) {error.textContent='Confirm that shared staff roles apply in Live and Rehearsal.';return;}
      error.textContent = '';
      this.busy = button.disabled = true;
      try {
        const result = await save({id:account.id,revision:account.revision,reason,...values()});
        form.dataset.accountDirty = '0';
        if (editor.isConnected) await reload(result);
      } catch (problem) {
        if (form.isConnected) error.textContent = problem.message;
      } finally {this.busy = button.disabled = false;}
    };
    grantsForm.onchange = update;
    grantsForm.onsubmit = submit(grantsForm,()=>({action:'roles',grants:roleValues(),...(sharedChanged()?{confirm_shared_roles:true}:{})}));
    if (link) {
      const source = () => link.elements.namedItem('source').value === '' ? null : linkSources[Number(link.elements.namedItem('source').value)];
      link.onchange = () => {
        const selected = source();
        const combined = selected && [...new Set([...account.grants,...selected.grants].map(grant=>roleLabels.get(grant.role)))];
        link.querySelector('[data-link-preview]').textContent = combined ? 'Combined dashboard access: ' + (combined.join(', ') || 'No dashboards') : '';
      };
      link.onsubmit = submit(link,()=>({action:'link',source_id:source().id,source_revision:source().revision,confirm:link.elements.namedItem('confirm').value}));
    }
    editor.querySelectorAll('form').forEach(form=>form.oninput=()=>{form.dataset.accountDirty='1';});
    update();
  }
};
