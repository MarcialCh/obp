SELECT 
	conference.name,
    save_circuit(provenance(), 'conference_id', '{path}')
FROM 
	author, writes, publication, conference 
WHERE
	author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.cid = conference.cid
    AND publication.year > 2010
    AND (author.name = 'Tova Milo' or author.name = 'H. V. Jagadish')
GROUP BY conference.name 