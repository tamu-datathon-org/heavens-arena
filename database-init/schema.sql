CREATE TABLE agents (
    agent_id SERIAL PRIMARY KEY,
    team_name VARCHAR(100) NOT NULL UNIQUE,
    image_name VARCHAR(255) NOT NULL,
    wins INT DEFAULT 0,
    losses INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE matches (
    match_id SERIAL PRIMARY KEY,
    agent_a_id INT NOT NULL REFERENCES agents(agent_id),
    agent_b_id INT NOT NULL REFERENCES agents(agent_id),
    winner_id INT REFERENCES agents(agent_id),
    played_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
