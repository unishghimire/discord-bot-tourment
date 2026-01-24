require('dotenv').config();
const { 
    Client, GatewayIntentBits, SlashCommandBuilder, REST, 
    Routes, EmbedBuilder, PermissionFlagsBits, ActivityType 
} = require('discord.js');
const express = require('express');
const fs = require('fs');
const bodyParser = require('body-parser');

const CONFIG = {
    TOKEN: process.env.BOT_TOKEN,
    CLIENT_ID: process.env.CLIENT_ID,
    PORT: process.env.PORT || 3000,
    DATA_FILE: './tournament_data.json'
};

// Database Initialization
let db = { templates: [], tournaments: [], teams: [], submissions: [] };
if (fs.existsSync(CONFIG.DATA_FILE)) {
    db = JSON.parse(fs.readFileSync(CONFIG.DATA_FILE));
}
const saveData = () => fs.writeFileSync(CONFIG.DATA_FILE, JSON.stringify(db, null, 2));

const client = new Client({ intents: [GatewayIntentBits.Guilds] });

// --- COMMAND DEFINITIONS ---
const commands = [
    new SlashCommandBuilder().setName('template').setDescription('Manage scoring templates')
        .addSubcommand(s => s.setName('create').setDescription('Create template').addStringOption(o => o.setName('name').setRequired(true).setDescription('Name')).addIntegerOption(o => o.setName('kp').setRequired(true).setDescription('Kill Points')).addStringOption(o => o.setName('pp').setRequired(true).setDescription('Placement Pts (10,5,0)')).addIntegerOption(o => o.setName('size').setRequired(true).setDescription('Team Size')))
        .addSubcommand(s => s.setName('list').setDescription('List templates')),
    new SlashCommandBuilder().setName('tournament').setDescription('Manage tournaments')
        .addSubcommand(s => s.setName('create').setDescription('New tournament').addStringOption(o => o.setName('name').setRequired(true)).addStringOption(o => o.setName('template').setRequired(true)).addIntegerOption(o => o.setName('matches').setRequired(true)))
        .addSubcommand(s => s.setName('start').setDescription('Start it')).addSubcommand(s => s.setName('leaderboard').setDescription('Show rank')),
    new SlashCommandBuilder().setName('register').setDescription('Join tournament').addStringOption(o => o.setName('team').setRequired(true)),
    new SlashCommandBuilder().setName('submit').setDescription('Report match')
        .addIntegerOption(o => o.setName('match').setRequired(true)).addIntegerOption(o => o.setName('rank').setRequired(true)).addIntegerOption(o => o.setName('kills').setRequired(true)).addAttachmentOption(o => o.setName('proof').setRequired(true))
];

// --- BOT LOGIC ---
client.on('interactionCreate', async (interaction) => {
    if (!interaction.isChatInputCommand()) return;

    // Template Logic
    if (interaction.commandName === 'template') {
        if (interaction.options.getSubcommand() === 'create') {
            const t = { id: Date.now().toString(), name: interaction.options.getString('name'), kp: interaction.options.getInteger('kp'), pp: interaction.options.getString('pp').split(',').map(Number), size: interaction.options.getInteger('size') };
            db.templates.push(t); saveData();
            return interaction.reply(`✅ Template ${t.name} saved!`);
        }
    }

    // Tournament Logic
    if (interaction.commandName === 'tournament') {
        const sub = interaction.options.getSubcommand();
        if (sub === 'create') {
            const tourney = { id: Date.now().toString(), name: interaction.options.getString('name'), template: interaction.options.getString('template'), status: 'REG', guild: interaction.guildId };
            db.tournaments.push(tourney); saveData();
            return interaction.reply(`🏆 Tournament ${tourney.name} is open for /register!`);
        }
        if (sub === 'leaderboard') {
            const t = db.tournaments.find(x => x.guild === interaction.guildId);
            if (!t) return interaction.reply("No tournament found.");
            const board = db.teams.filter(x => x.tid === t.id).map(team => {
                const pts = db.submissions.filter(s => s.teamId === team.id && s.status === 'APPROVED').reduce((acc, s) => acc + s.kills + (db.templates.find(temp => temp.name === t.template).pp[s.rank-1] || 0), 0);
                return `${team.name}: ${pts} pts`;
            }).join('\n') || "No scores yet.";
            return interaction.reply(`**Standings:**\n${board}`);
        }
    }

    // Register Logic
    if (interaction.commandName === 'register') {
        const t = db.tournaments.find(x => x.guild === interaction.guildId && x.status === 'REG');
        if (!t) return interaction.reply("No active registration.");
        db.teams.push({ id: Date.now().toString(), name: interaction.options.getString('team'), captain: interaction.user.id, tid: t.id });
        saveData();
        return interaction.reply("✅ Registered!");
    }

    // Submit Logic
    if (interaction.commandName === 'submit') {
        const team = db.teams.find(x => x.captain === interaction.user.id);
        if (!team) return interaction.reply("Only captains can submit.");
        db.submissions.push({ id: Date.now().toString(), teamId: team.id, rank: interaction.options.getInteger('rank'), kills: interaction.options.getInteger('kills'), proof: interaction.options.getAttachment('proof').url, status: 'PENDING' });
        saveData();
        return interaction.reply("🚀 Result submitted for review!");
    }
});

// --- WEB PANEL ---
const app = express();
app.use(bodyParser.json());
app.get('/', (req, res) => {
    let rows = db.submissions.map(s => `<tr><td>${s.rank}</td><td>${s.kills}</td><td><a href="${s.proof}">Link</a></td><td>${s.status}</td><td><button onclick="approve('${s.id}')">Approve</button></td></tr>`).join('');
    res.send(`<h1>Admin Panel</h1><table border="1"><tr><th>Rank</th><th>Kills</th><th>Proof</th><th>Status</th><th>Action</th></tr>${rows}</table>
    <script>function approve(id){ fetch('/approve/'+id, {method:'POST'}).then(()=>location.reload()) }</script>`);
});
app.post('/approve/:id', (req, res) => {
    const s = db.submissions.find(x => x.id === req.params.id);
    if(s) s.status = 'APPROVED'; saveData(); res.sendStatus(200);
});

// --- INIT ---
client.once('ready', () => {
    console.log('Bot is Online!');
    client.user.setActivity('Tournaments', { type: ActivityType.Competing });
});

const rest = new REST({ version: '10' }).setToken(CONFIG.TOKEN);
(async () => {
    await rest.put(Routes.applicationCommands(CONFIG.CLIENT_ID), { body: commands });
    client.login(CONFIG.TOKEN);
    app.listen(CONFIG.PORT, () => console.log(`Dashboard: http://localhost:${CONFIG.PORT}`));
})();
