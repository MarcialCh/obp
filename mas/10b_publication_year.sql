SELECT 
    publication.year,
    save_circuit(provenance(), 'publication_id', '{path}')
FROM 
    publication, conference
WHERE
    publication.title LIKE 'Making %'
    AND publication.cid = conference.cid
    AND conference.name = 'VLDB'
GROUP BY 
    publication.year
LIMIT 10